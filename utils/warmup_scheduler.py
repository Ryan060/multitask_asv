## Copied from https://github.com/ildoonet/pytorch-gradual-warmup-lr/blob/master/warmup_scheduler/scheduler.py

from torch.optim.lr_scheduler import _LRScheduler
from torch.optim.lr_scheduler import ReduceLROnPlateau


class GradualWarmupScheduler(_LRScheduler):
	""" Gradually warm-up(increasing) learning rate in optimizer.
	Proposed in 'Accurate, Large Minibatch SGD: Training ImageNet in 1 Hour'.

	Args:
		optimizer (Optimizer): Wrapped optimizer.
		multiplier: target learning rate = base lr * multiplier
		total_epoch: target learning rate is reached at total_epoch, gradually
		after_scheduler: after target_epoch, use this scheduler(eg. ReduceLROnPlateau)
	"""

	def __init__(self, optimizer, total_epoch, init_lr=1e-7, after_scheduler=None):
		self.init_lr = multinit_lriplier
		assert init_lr>0, 'Initial LR should be greater than 0.'
		self.total_epoch = total_epoch
		self.after_scheduler = after_scheduler
		self.finished = False
		super().__init__(optimizer)

	def get_lr(self):
		if self.last_epoch > self.total_epoch:
			if self.after_scheduler:
				if not self.finished:
					self.finished = True
				return self.after_scheduler.get_lr()
			return self.base_lrs

		return [(((base_lr - self.init_lr)/self.total_epoch) * self.last_epoch + self.init_lr) for base_lr in self.base_lrs]

	def step_ReduceLROnPlateau(self, metrics, epoch=None):
		if epoch is None:
			epoch = self.last_epoch + 1
		self.last_epoch = epoch if epoch != 0 else 1  # ReduceLROnPlateau is called at the end of epoch, whereas others are called at beginning
		if self.last_epoch <= self.total_epoch:
			warmup_lr = [base_lr * ((self.multiplier - 1.) * self.last_epoch / self.total_epoch + 1.) for base_lr in self.base_lrs]
			for param_group, lr in zip(self.optimizer.param_groups, warmup_lr):
				param_group['lr'] = lr
		else:
			if epoch is None:
				self.after_scheduler.step(metrics, None)
			else:
				self.after_scheduler.step(metrics, epoch - self.total_epoch)

	def step(self, epoch=None, metrics=None):
		if type(self.after_scheduler) != ReduceLROnPlateau:
			if (self.finished and self.after_scheduler) or self.total_epoch==0:
				if epoch is None:
					self.after_scheduler.step(None)
				else:
					self.after_scheduler.step(epoch - self.total_epoch)
			else:
				return super(GradualWarmupScheduler, self).step(epoch)
		else:
			self.step_ReduceLROnPlateau(metrics, epoch)
