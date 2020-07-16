"""
Dice loss 3D
"""

import torch
import torch.nn as nn
from torch.nn import functional as F

def agg_dice_score(preds, targets):
  ''' Gives Dice score for sub-regions which are evaluated in the
  competition.
  '''
  channel_shape = preds[:, 0, :, :, :].size()

  agg = torch.zeros(preds.size())
  et = torch.zeros(channel_shape)
  et[torch.where(preds[:, 2, :, :, :] > 0.5)] = 1
  et = et.unsqueeze(1)
  
  wt = torch.zeros(channel_shape)
  wt[torch.where((preds[:, 0, :, :, :] > 0.5) | 
  (preds[:, 1, :, :, :] > 0.5) | 
  (preds[:, 2, :, :, :] > 0.5) )] = 1
  wt = wt.unsqueeze(1)

  tc = torch.zeros(channel_shape)
  tc[torch.where((preds[:, 0, :, :, :] > 0.5) | (preds[:, 2, :, :, :] > 0.5) )] = 1
  tc = tc.unsqueeze(1)
  
  agg_preds = torch.cat((et, wt, tc), 1)

  et_target = torch.zeros(channel_shape)
  et_target[torch.where(targets[:, 2, :, :, :] > 0.5)] = 1
  et_target = et_target.unsqueeze(1) 

  wt_target = torch.zeros(channel_shape)
  wt_target[torch.where((targets[:, 0, :, :, :] > 0.5) | 
  (targets[:, 1, :, :, :] > 0.5) | (targets[:, 2, :, :, :] > 0.5) )] = 1
  wt_target = wt_target.unsqueeze(1)
  
  tc_target = torch.zeros(channel_shape)
  tc_target[torch.where((targets[:, 0, :, :, :] > 0.5) | 
  (targets[:, 2, :, :, :] > 0.5) )] = 1
  tc_target = tc_target.unsqueeze(1)

  agg_targets = torch.cat((et_target, wt_target, tc_target), 1)

  return dice_score(agg_preds, agg_targets)

def dice_score(preds, targets):
    # have to cast to float because of issues with amp
    targets_fp = targets.float()
    preds_fp = preds.float()

    num = 2*torch.einsum('bcijk, bcijk ->bc', [preds_fp, targets_fp])
    denom = torch.einsum('bcijk, bcijk -> bc', [preds_fp, preds_fp]) +\
        torch.einsum('bcijk, bcijk -> bc', [targets_fp, targets_fp]) + 1e-32
    proportions = torch.div(num, denom) 
    return torch.einsum('bc->c', proportions)


class KLLoss(nn.Module):
  def __init__(self):
    super(KLLoss, self).__init__()

  def forward(self, mu, logvar, N):
    sum_square_mean = torch.einsum('i,i->', mu, mu)
    sum_log_var = torch.einsum('i->', logvar)
    sum_var = torch.einsum('i->', torch.exp(logvar))
    
    return float(1/N)*(sum_square_mean+sum_var-sum_log_var-N)


class VAEDiceLoss(nn.Module):
  def __init__(self, label_recon = False):
    super(VAEDiceLoss, self).__init__()
    self.dice = AvgDiceLoss()
    self.kl = KLLoss()
    self.label_recon = label_recon

  def forward(self, output, target):
    return self.dice(output['seg_map'], target['target'])\
        + 0.1*F.mse_loss(output['recon'], target['src'])\
        + 0.1*self.kl(output['mu'], output['logvar'], 256)

class AvgDiceLoss(nn.Module):
  def __init__(self):
    super(AvgDiceLoss, self).__init__()

  def forward(self, preds, targets):
    target = targets['target']
    proportions = dice_score(preds, target)
    avg_dice = torch.einsum('c->', proportions) / (target.shape[0]*target.shape[1])
    return 1 - avg_dice

class CascadeAvgDiceLoss(nn.Module):
    def __init__(self):
        super(CascadeAvgDiceLoss, self).__init__()
        self.coarse_loss = AvgDiceLoss()
        self.deconv_loss = AvgDiceLoss()
        self.biline_loss = AvgDiceLoss()

    def forward(self, output, targets):
        return 0.33*(self.coarse_loss(output['coarse'], targets)\
                + self.biline_loss(output['biline'], targets)\
                + self.deconv_loss(output['deconv'], targets))

class DiceLoss(nn.Module):
  def __init__(self):
    super(DiceLoss, self).__init__()

  def forward(self, preds, targets):
    target = targets['target']
    proportions = dice_score(preds, target)
    return -torch.einsum('c->', proportions) / target.shape[0]

class ReconRegLoss(nn.Module):
  def __init__(self):
    super(ReconRegLoss, self).__init__()
    self.dice = AvgDiceLoss()

  def forward(self, output_targets):
    output, target, src = output_targets
    preds, recon = output
    dice_loss = self.dice(output_targets)
    return dice_loss + 0.1*F.mse_loss(recon, src)

def build(loss):
  if loss == 'dice':
    return DiceLoss()
  if loss == 'recon':
    return ReconRegLoss()
  if loss == 'avgdice':
    return AvgDiceLoss()
  if loss == 'vae':
    return VAEDiceLoss()
