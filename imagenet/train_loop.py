import torch
import torch.nn.functional as F

import numpy as np

import os
from tqdm import tqdm

from harvester import HardestNegativeTripletSelector, AllTripletSelector
from models.losses import LabelSmoothingLoss
from utils import compute_eer, adjust_learning_rate, correct_topk
from data_load import Loader

class TrainLoop(object):

	def __init__(self, model, optimizer, train_loader, valid_loader, margin, lambda_, max_gnorm, patience, lr_factor, label_smoothing, verbose=-1, cp_name=None, save_cp=False, checkpoint_path=None, checkpoint_epoch=None, pretrain=False, swap=False, cuda=True, logger=None):
		if checkpoint_path is None:
			# Save to current directory
			self.checkpoint_path = os.getcwd()
		else:
			self.checkpoint_path = checkpoint_path
			if not os.path.isdir(self.checkpoint_path):
				os.mkdir(self.checkpoint_path)

		self.save_epoch_fmt = os.path.join(self.checkpoint_path, cp_name) if cp_name else os.path.join(self.checkpoint_path, 'checkpoint_{}ep.pt')
		self.cuda_mode = cuda
		self.pretrain = pretrain
		self.model = model
		self.optimizer = optimizer
		self.patience = patience
		self.max_gnorm = max_gnorm
		self.lr_factor = lr_factor
		self.lambda_ = lambda_
		self.swap = swap
		self.margin = margin
		self.train_loader = train_loader
		self.valid_loader = valid_loader
		self.total_iters = 0
		self.cur_epoch = 0
		self.harvester = HardestNegativeTripletSelector(margin=0.1, cpu=not self.cuda_mode)
		self.harvester_val = AllTripletSelector()
		self.verbose = verbose
		self.save_cp = save_cp
		self.device = next(self.model.parameters()).device
		self.history = {'train_loss': [], 'train_loss_batch': [], 'ce_loss': [], 'ce_loss_batch': [], 'triplet_loss': [], 'triplet_loss_batch': []}
		self.disc_label_smoothing = label_smoothing*0.5
		self.base_lr = self.optimizer.param_groups[0]['lr']
		self.logger = logger

		if label_smoothing>0.0:
			self.ce_criterion = LabelSmoothingLoss(label_smoothing, lbl_set_size=1000)
		else:
			self.ce_criterion = torch.nn.CrossEntropyLoss()

		if self.valid_loader is not None:
			self.history['cos_eer'] = []
			self.history['acc_1'] = []
			self.history['acc_5'] = []

		if checkpoint_epoch is not None:
			self.load_checkpoint(self.save_epoch_fmt.format(checkpoint_epoch))

	def train(self, n_epochs=1, save_every=1):

		while (self.cur_epoch < n_epochs):

			np.random.seed()
			if isinstance(self.train_loader.dataset, Loader):
				self.train_loader.dataset.update_lists()

			adjust_learning_rate(self.optimizer, self.cur_epoch, self.base_lr, self.patience, self.lr_factor)

			if self.verbose>1:
				print(' ')
				print('Epoch {}/{}'.format(self.cur_epoch+1, n_epochs))
				train_iter = tqdm(enumerate(self.train_loader))
			else:
				train_iter = enumerate(self.train_loader)

			if self.pretrain:

				ce_epoch=0.0
				for t, batch in train_iter:
					ce = self.pretrain_step(batch)
					self.history['train_loss_batch'].append(ce)
					ce_epoch+=ce
					self.logger.add_scalar('Train/Cross entropy', ce, self.total_iters)
					self.logger.add_scalar('Info/LR', self.optimizer.param_groups[0]['lr'], self.total_iters)
					self.total_iters += 1

				self.history['train_loss'].append(ce_epoch/(t+1))

				if self.verbose>1:
					print('Train loss: {:0.4f}'.format(self.history['train_loss'][-1]))

			else:

				train_loss_epoch=0.0
				ce_loss_epoch=0.0
				triplet_loss_epoch=0.0
				for t, batch in train_iter:
					train_loss, ce_loss, triplet_loss = self.train_step(batch)
					self.history['train_loss_batch'].append(train_loss)
					self.history['ce_loss_batch'].append(ce_loss)
					self.history['triplet_loss_batch'].append(triplet_loss)
					train_loss_epoch+=train_loss
					ce_loss_epoch+=ce_loss
					triplet_loss_epoch+=triplet_loss
					if self.logger:
						self.logger.add_scalar('Train/Total train Loss', train_loss, self.total_iters)
						self.logger.add_scalar('Train/Triplet Loss', triplet_loss, self.total_iters)
						self.logger.add_scalar('Train/Cross enropy', ce_loss, self.total_iters)
						self.logger.add_scalar('Info/LR', self.optimizer.param_groups[0]['lr'], self.total_iters)
					self.total_iters += 1

				self.history['train_loss'].append(train_loss_epoch/(t+1))
				self.history['ce_loss'].append(ce_loss_epoch/(t+1))
				self.history['triplet_loss'].append(triplet_loss_epoch/(t+1))

				if self.verbose>1:
					print(' ')
					print('Total train loss: {:0.4f}'.format(self.history['train_loss'][-1]))
					print('CE loss: {:0.4f}'.format(self.history['ce_loss'][-1]))
					print('triplet_loss loss: {:0.4f}'.format(self.history['triplet_loss'][-1]))
					print(' ')

			if self.valid_loader is not None:

				tot_correct_1, tot_correct_5, tot_ = 0, 0, 0
				cos_scores, labels = None, None

				for t, batch in enumerate(self.valid_loader):
					correct_1, correct_5, total, cos_scores_batch, labels_batch = self.valid(batch)

					try:
						cos_scores = np.concatenate([cos_scores, cos_scores_batch], 0)
						labels = np.concatenate([labels, labels_batch], 0)
					except:
						cos_scores, labels = cos_scores_batch, labels_batch

					tot_correct_1 += correct_1
					tot_correct_5 += correct_5
					tot_ += total

				self.history['cos_eer'].append(compute_eer(labels, cos_scores))
				self.history['acc_1'].append(float(tot_correct_1)/tot_)
				self.history['acc_5'].append(float(tot_correct_5)/tot_)
				if self.logger:
					self.logger.add_scalar('Valid/Cosine EER', self.history['cos_eer'][-1], self.total_iters-1)
					self.logger.add_scalar('Valid/Best Cosine EER', np.min(self.history['cos_eer']), self.total_iters-1)
					self.logger.add_scalar('Valid/ACC-1', self.history['acc_1'][-1], self.total_iters-1)
					self.logger.add_scalar('Valid/Best ACC-1', np.max(self.history['acc_1']), self.total_iters-1)
					self.logger.add_scalar('Valid/ACC-5', self.history['acc_5'][-1], self.total_iters-1)
					self.logger.add_scalar('Valid/Best ACC-5', np.max(self.history['acc_5']), self.total_iters-1)
					self.logger.add_pr_curve('Cosine ROC', labels=labels, predictions=cos_scores, global_step=self.total_iters-1)
					self.logger.add_histogram('Valid/COS_Scores', values=cos_scores, global_step=self.total_iters-1)
					self.logger.add_histogram('Valid/Labels', values=labels, global_step=self.total_iters-1)

				if self.verbose>1:
					print(' ')
					print('Current cos EER, best cos EER, and epoch: {:0.4f}, {:0.4f}, {}'.format(self.history['cos_eer'][-1], np.min(self.history['cos_eer']), 1+np.argmin(self.history['cos_eer'])))
					print('Current Top 1 Acc, best Top 1 Acc, and epoch: {:0.4f}, {:0.4f}, {}'.format(self.history['acc_1'][-1], np.max(self.history['acc_1']), 1+np.argmax(self.history['acc_1'])))
					print('Current Top 5 Acc, best Top 5 Acc, and epoch: {:0.4f}, {:0.4f}, {}'.format(self.history['acc_5'][-1], np.max(self.history['acc_5']), 1+np.argmax(self.history['acc_5'])))

			if self.verbose>1:
				print('Current LR: {}'.format(self.optimizer.param_groups[0]['lr']))

			self.cur_epoch += 1

			if self.valid_loader is not None and self.save_cp and (self.cur_epoch % save_every == 0 or self.history['cos_eer'][-1] < np.min([np.inf]+self.history['cos_eer'][:-1])):
					self.checkpointing()
			elif self.save_cp and self.cur_epoch % save_every == 0:
					self.checkpointing()

		if self.verbose>1:
			print('Training done!')

		if self.valid_loader is not None:
			if self.verbose>1:
				print('Best cos eer and corresponding epoch: {:0.4f}, {}'.format(np.min(self.history['cos_eer']), 1+np.argmin(self.history['cos_eer'])))

			return [np.min(self.history['cos_eer'])]
		else:
			return [np.min(self.history['train_loss'])]

	def train_step(self, batch):

		self.model.train()
		self.optimizer.zero_grad()

		if isinstance(self.train_loader.dataset, Loader):
			x_1, x_2, x_3, x_4, x_5, y = batch
			x = torch.cat([x_1, x_2, x_3, x_4, x_5], dim=0)
			y = torch.cat(5*[y], dim=0).squeeze().contiguous()
		else:
			x, y = batch

		x = x.to(self.device, non_blocking=True)
		y = y.to(self.device, non_blocking=True)

		embeddings, out = self.model.forward(x)
		embeddings_norm = F.normalize(embeddings, p=2, dim=1)

		ce_loss = self.ce_criterion(self.model.out_proj(out, y), y)

		# Get all triplets now for bin classifier
		triplets_idx, entropy_indices = self.harvester.get_triplets(embeddings_norm.detach(), y)
		triplets_idx = triplets_idx.to(self.device, non_blocking=True)

		emb_a = torch.index_select(embeddings_norm, 0, triplets_idx[:, 0])
		emb_p = torch.index_select(embeddings_norm, 0, triplets_idx[:, 1])
		emb_n = torch.index_select(embeddings_norm, 0, triplets_idx[:, 2])

		loss_metric = self.triplet_loss(emb_a, emb_p, emb_n)

		loss = ce_loss + loss_metric

		entropy_regularizer = torch.nn.functional.pairwise_distance(embeddings_norm, embeddings_norm[entropy_indices,:]).mean()
		loss -= entropy_regularizer*self.lambda_

		loss.backward()
		grad_norm = torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.max_gnorm)
		self.optimizer.step()

		if self.logger:
			self.logger.add_scalar('Info/Grad_norm', grad_norm, self.total_iters)

		return loss.item(), ce_loss.item(), loss_metric.item()


	def pretrain_step(self, batch):

		self.model.train()
		self.optimizer.zero_grad()

		x, y = batch

		x, y = x.to(self.device, non_blocking=True), y.to(self.device, non_blocking=True).squeeze()

		embeddings, out = self.model.forward(utt)

		loss = F.cross_entropy(self.model.out_proj(out, y), y)

		loss.backward()
		self.optimizer.step()
		grad_norm = torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.max_gnorm)

		if self.logger:
			self.logger.add_scalar('Info/Grad_norm', grad_norm, self.total_iters)

		return loss.item()


	def valid(self, batch):

		self.model.eval()

		with torch.no_grad():

			if isinstance(self.valid_loader.dataset, Loader):
				x_1, x_2, x_3, x_4, x_5, y = batch
				x = torch.cat([x_1, x_2, x_3, x_4, x_5], dim=0)
				y = torch.cat(5*[y], dim=0).squeeze().contiguous()
			else:
				x, y = batch

			x = x.to(self.device, non_blocking=True)
			y = y.to(self.device, non_blocking=True)

			embeddings, out = self.model.forward(x)
			embeddings_norm = F.normalize(embeddings, p=2, dim=1)

			out = self.model.out_proj(out, y)

			pred = F.softmax(out, dim=1)
			(correct_1, correct_5) = correct_topk(pred, y, (1,5))

			# Get all triplets now for bin classifier
			triplets_idx = self.harvester_val.get_triplets(embeddings.detach(), y)
			triplets_idx = triplets_idx.to(self.device, non_blocking=True)

			emb_a = torch.index_select(embeddings_norm, 0, triplets_idx[:, 0])
			emb_p = torch.index_select(embeddings_norm, 0, triplets_idx[:, 1])
			emb_n = torch.index_select(embeddings_norm, 0, triplets_idx[:, 2])

			cos_scores_p = torch.nn.functional.cosine_similarity(emb_a, emb_p)
			cos_scores_n = torch.nn.functional.cosine_similarity(emb_a, emb_n)

		return correct_1, correct_5, x.size(0), np.concatenate([cos_scores_p.detach().cpu().numpy(), cos_scores_n.detach().cpu().numpy()], 0), np.concatenate([np.ones(cos_scores_p.size(0)), np.zeros(cos_scores_p.size(0))], 0)

	def triplet_loss(self, emba, embp, embn, reduce_=True):

		loss_ = torch.nn.TripletMarginLoss(margin=self.margin, p=2.0, eps=1e-06, swap=self.swap, reduction='mean' if reduce_ else 'none')(emba, embp, embn)

		return loss_

	def checkpointing(self):

		# Checkpointing
		if self.verbose>1:
			print('Checkpointing...')
		ckpt = {'model_state': self.model.state_dict(),
		'optimizer_state': self.optimizer.state_dict(),
		'history': self.history,
		'total_iters': self.total_iters,
		'cur_epoch': self.cur_epoch}
		try:
			torch.save(ckpt, self.save_epoch_fmt.format(self.cur_epoch))
		except:
			torch.save(ckpt, self.save_epoch_fmt)

	def load_checkpoint(self, ckpt):

		if os.path.isfile(ckpt):

			ckpt = torch.load(ckpt, map_location = lambda storage, loc: storage)
			# Load model state
			self.model.load_state_dict(ckpt['model_state'])
			# Load optimizer state
			self.optimizer.load_state_dict(ckpt['optimizer_state'])
			# Load history
			self.history = ckpt['history']
			self.total_iters = ckpt['total_iters']
			self.cur_epoch = ckpt['cur_epoch']
			if self.cuda_mode:
				self.model = self.model.cuda(self.device)

		else:
			print('No checkpoint found at: {}'.format(ckpt))

	def print_grad_norms(self):
		norm = 0.0
		for params in list(self.model.parameters()):
			norm+=params.grad.norm(2).item()
		print('Sum of grads norms: {}'.format(norm))
