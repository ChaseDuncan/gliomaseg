import os
import sys
import tabulate

import torch
import torch.optim as optim
import torch.nn as nn
import numpy as np
#from torch.utils.tensorboard import SummaryWriter
import pickle
import argparse
import random
from utils import (
    save_checkpoint,
    load_data,
    train,
    validate,
    train_epoch,
    validate_bg
    )

from torch.utils.data import DataLoader
from scheduler import PolynomialLR
import losses
from models.models import *
from bg_dataloader import *

import time
parser = argparse.ArgumentParser(description='Train glioma segmentation model.')

# In this directory is stored the script used to start the training,
# the most recent and best checkpoints, and a directory of logs.
parser.add_argument('--dir', type=str, required=True, metavar='PATH',
    help='The directory to write all output to.')

parser.add_argument('--data_dir', type=str, required=True, metavar='PATH TO DATA',
    help='Path to where the data is located.')

parser.add_argument('--model', type=str, default=None, required=True, metavar='MODEL',
                        help='model name (default: None)')

parser.add_argument('--upsampling', type=str, default='bilinear', 
    choices=['bilinear', 'deconv'], 
    help='upsampling algorithm to use in decoder (default: bilinear)')

parser.add_argument('--loss', type=str, default='avgdice', 
    choices=['dice', 'recon', 'avgdice', 'vae'], 
    help='which loss to use during training (default: avgdice)')

parser.add_argument('--seed', type=int, default=1, metavar='S', 
    help='random seed (default: 1)')

parser.add_argument('--wd', type=float, default=1e-4, 
    help='weight decay (default: 1e-4)')

parser.add_argument('--resume', type=str, default=None, metavar='PATH',
                        help='checkpoint to resume training from (default: None)')

parser.add_argument('--epochs', type=int, default=300, metavar='N', 
    help='number of epochs to train (default: 300)')

parser.add_argument('--batches_per_epoch', type=int, default=74, metavar='N', 
    help='number of batches to train per epoch (default: 74)')

parser.add_argument('--num_workers', type=int, default=4, metavar='N', 
    help='number of workers to assign to dataloader (default: 4)')

parser.add_argument('--batch_size', type=int, default=1, metavar='N', 
    help='batch_size (default: 1)')

parser.add_argument('--save_freq', type=int, default=25, metavar='N', 
    help='save frequency (default: 25)')

parser.add_argument('--eval_freq', type=int, default=5, metavar='N', 
    help='evaluation frequency (default: 25)')

parser.add_argument('--lr', type=float, default=1e-4, metavar='LR', 
    help='initial learning rate (default: 1e-4)')

parser.add_argument('--eclr', action='store_true', 
    help='step clr per epoch(default: off)')

parser.add_argument('--single_threaded', action='store_true', 
    help='use single_threaded dataloader for debug (default: off)')

# Currently unused.
parser.add_argument('--momentum', type=float, default=0.9, metavar='M', 
    help='SGD momentum (default: 0.9)')

args = parser.parse_args()

device = torch.device('cuda:1')

os.makedirs(f'{args.dir}/logs', exist_ok=True)
os.makedirs(f'{args.dir}/checkpoints', exist_ok=True)

with open(os.path.join(args.dir, 'command.sh'), 'w') as f:
  f.write(' '.join(sys.argv))
  f.write('\n')

# batch_gen variables
num_threads_for_brats_example = args.num_workers
brats_preprocessed_folder = args.data_dir

patients = get_list_of_patients(brats_preprocessed_folder)
#train, val = get_split_deterministic(patients, fold=0, num_splits=5, random_state=12345)
train = patients
patch_size = (128, 128, 128)
#patch_size = (160, 192, 128)
batch_size = args.batch_size
dataloader = BraTS2018DataLoader3D(train, batch_size, patch_size, 1)

batch = next(dataloader)
shapes = [BraTS2018DataLoader3D.load_patient(i)[0].shape[1:] for i in patients]
max_shape = np.max(shapes, 0)
max_shape = np.max((max_shape, patch_size), 0)

dataloader_train = BraTS2018DataLoader3D(
        train, 
        batch_size, 
        max_shape, 
        num_threads_for_brats_example
        )

dataloader_validation = BraTS2018DataLoader3D(
        train, 
        batch_size, 
        patch_size, 
        max(1, num_threads_for_brats_example // 2)
        )


tr_transforms = get_train_transform(patch_size)
if args.single_threaded:
    tr_gen = SingleThreadedAugmenter(dataloader_train, tr_transforms)
    val_gen = SingleThreadedAugmenter(dataloader_validation, None)
else:
    tr_gen = MultiThreadedAugmenter(dataloader_train, tr_transforms, num_processes=num_threads_for_brats_example,
                                        num_cached_per_queue=3,
                                        seeds=None, pin_memory=False)
    val_gen = MultiThreadedAugmenter(dataloader_validation, None,
                                         num_processes=max(1, num_threads_for_brats_example // 2), 
                                         num_cached_per_queue=1,
                                         seeds=None,
                                         pin_memory=False)


if args.model == 'MonoUNet':
    model = MonoUNet()

model = model.to(device)

optimizer = \
    optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)

start_epoch = 0
if args.resume:
  print("Resume training from %s" % args.resume)
  checkpoint = torch.load(args.resume)
  start_epoch = checkpoint["epoch"]
  model.load_state_dict(checkpoint["state_dict"])
  optimizer.load_state_dict(checkpoint["optimizer"])    

# TODO: optimizer factory, allow for SGD with momentum etx.
columns = ['ep', 'loss', 'dice_tc_agg',\
  'dice_et_agg', 'dice_ed_agg', 'dice_ncr', 'dice_et',\
  'dice_wt', 'time', 'mem_usage']

#writer = SummaryWriter(log_dir=f'{args.dir}/logs')
scheduler = PolynomialLR(optimizer, args.epochs)
loss = losses.AvgDiceLoss()

for epoch in range(start_epoch, args.epochs):
    time_ep = time.time()
    model.train()

    train_epoch(model, loss, optimizer, tr_gen, args.batches_per_epoch, device)
    
    if (epoch + 1) % args.save_freq == 0:
        save_checkpoint(
                f'{args.dir}/checkpoints',
                epoch + 1,
                state_dict=model.state_dict(),
                optimizer=optimizer.state_dict()
                )
    
    if (epoch + 1) % args.eval_freq == 0:
        # Evaluate on training data
        train_res = validate_bg(model, loss, val_gen, args.batches_per_epoch, device)
        time_ep = time.time() - time_ep
        memory_usage = torch.cuda.memory_allocated() / (1024.0 ** 3)
        values = [epoch + 1, train_res['train_loss'].data] \
          +  \
          train_res['train_dice'].tolist()\
          + [ time_ep, memory_usage] 
        table = tabulate.tabulate([values], 
                columns, tablefmt="simple", floatfmt="8.4f")
        print(table)
    
    # Log validation
    #writer.add_scalar('Loss/train', train_loss, epoch)
    #writer.add_scalar('Dice/train/ncr&net', train_dice[0], epoch)
    #writer.add_scalar('Dice/train/ed', train_dice[1], epoch)
    #writer.add_scalar('Dice/train/et', train_dice[2], epoch)
    #writer.add_scalar('Dice/train/et_agg', train_dice_agg[0], epoch)
    #writer.add_scalar('Dice/train/wt_agg', train_dice_agg[1], epoch)
    #writer.add_scalar('Dice/train/tc_agg', train_dice_agg[2], epoch)
    
    scheduler.step()

