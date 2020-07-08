import os
import sys
import time
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
    train_epoch,
    validate,
    get_free_gpu
    )

from batchgenerators.utilities.data_splitting import get_split_deterministic
from batchgenerators.dataloading import MultiThreadedAugmenter, SingleThreadedAugmenter
from brats2018_dataloader import(
        get_list_of_patients,
        get_train_transform,
        BraTS2018DataLoader3D
        )
from scheduler import PolynomialLR
import losses
from models import *
from lean_net import LeaNet
from dropout_lean_net import DropoutLeaNet

parser = argparse.ArgumentParser(description='Train glioma segmentation model.')

# In this directory is stored the script used to start the training,
# the most recent and best checkpoints, and a directory of logs.
parser.add_argument('--dir', type=str, required=True, metavar='PATH',
    help='The directory to write all output to.')

parser.add_argument('--data_dir', type=str, required=True, metavar='PATH TO DATA',
    help='Path to where the data is located.')

parser.add_argument('--model', type=str, default=None, required=True, metavar='MODEL',
                        help='model name (default: None)')

parser.add_argument('--device', type=int, default=-1, metavar='n')
parser.add_argument('--upsampling', type=str, default='bilinear', 
    choices=['bilinear', 'deconv'], 
    help='upsampling algorithm to use in decoder (default: bilinear)')

parser.add_argument('--loss', type=str, default='avgdice', 
    choices=['dice', 'recon', 'avgdice', 'vae'], 
    help='which loss to use during training (default: avgdice)')

parser.add_argument('--data_par', action='store_true', 
    help='data parellelism flag (default: off)')

parser.add_argument('--dropout', action='store_true', 
    help='do not train with dropout (default: train with dropout)')

parser.add_argument('--baseline', action='store_true', 
    help='Use the baseline model (default: false)')

parser.add_argument('--single_threaded', action='store_true', 
    help='Single threaded data loading for debgging(default: multithreaded)')

parser.add_argument('--seed', type=int, default=1, metavar='S', 
    help='random seed (default: 1)')

parser.add_argument('--wd', type=float, default=1e-4, 
    help='weight decay (default: 1e-4)')

parser.add_argument('--resume', type=str, default=None, metavar='PATH',
                        help='checkpoint to resume training from (default: None)')

parser.add_argument('--epochs', type=int, default=300, metavar='N', 
    help='number of epochs to train (default: 300)')

parser.add_argument('--num_threads', type=int, default=4, metavar='N', 
    help='number of workers to assign to dataloader (default: 4)')

parser.add_argument('--batch_size', type=int, default=1, metavar='N', 
    help='batch_size (default: 1)')

parser.add_argument('--save_freq', type=int, default=25, metavar='N', 
    help='save frequency (default: 25)')

parser.add_argument('--batches_per_epoch', type=int, default=70, metavar='N', 
    help='how many batches to use for training per epoch (default: 70)')

parser.add_argument('--eval_freq', type=int, default=5, metavar='N', 
    help='evaluation frequency (default: 25)')

parser.add_argument('--lr', type=float, default=1e-4, metavar='LR', 
    help='initial learning rate (default: 1e-4)')

# Currently unused.
parser.add_argument('--momentum', type=float, default=0.9, metavar='M', 
    help='SGD momentum (default: 0.9)')

args = parser.parse_args()
if args.device < 0:
    args.device = get_free_gpu()

print(f'Using device {args.device}.')

device = torch.device(f'cuda:{args.device}')
#os.makedirs(f'{args.dir}/logs', exist_ok=True)
os.makedirs(f'{args.dir}/checkpoints', exist_ok=True)

with open(os.path.join(args.dir, 'command.sh'), 'w') as f:
  f.write(' '.join(sys.argv))
  f.write('\n')

torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)
np.random.seed(args.seed)
random.seed(args.seed)
torch.manual_seed(args.seed)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True

# TODO: move data into /dev/shm

dims=[160, 192, 128]
#dims=[128, 128, 128]
brats_data = BraTSDataset(args.data_dir, dims=dims)
trainloader = DataLoader(brats_data, batch_size=args.batch_size, 
                        shuffle=True, num_workers=args.num_workers)

#model = UNet(cfg)
#if args.no_dropout:
#    model = MonoUNet(dropout=False)
#else:
#    model = MonoUNet()
#model = models_min.UNet()

if args.dropout:
    print(f'Using architecture DropoutLeaNet.')
    model = DropoutLeaNet()
elif not args.baseline:
    print(f'Using architecture LeaNet.')
    model = LeaNet()
elif args.baseline:
    print(f'Using architecture MonoUNet.')
    model = MonoUNet()

#device_ids = [i for i in range(torch.cuda.device_count())]
#model = nn.DataParallel(model, device_ids)

#if args.data_par:
#    device = torch.device('cuda:1')
#    model = nn.DataParallel(model, [1, 2])

model = model.to(device)

optimizer = \
    optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)

start_epoch = 0
if args.resume:
  checkpoint = torch.load(args.resume)
  start_epoch = checkpoint["epoch"]
  model.load_state_dict(checkpoint["state_dict"])
  optimizer.load_state_dict(checkpoint["optimizer"])    
  print(f"Resume training from {args.resume} from epoch {start_epoch}.")

# TODO: optimizer factory, allow for SGD with momentum etx.
#columns = ['ep', 'loss', 'dice_tc_agg',\
#  'dice_et_agg', 'dice_ed_agg', 'dice_ncr', 'dice_et',\
#  'dice_wt', 'time', 'mem_usage']

columns = ['ep', 'loss', 'dice_et', 'dice_wt', 'dice_tc', 'time', 'mem_usage']

#writer = SummaryWriter(log_dir=f'{args.dir}/logs')
scheduler = PolynomialLR(optimizer, args.epochs)
loss = losses.build(args.loss)

print('Beginning training.')
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
            train_res = validate(model, loss, trainloader, device)
            time_ep = time.time() - time_ep
            memory_usage = torch.cuda.memory_allocated() / (1024.0 ** 3)
            values = [epoch + 1, train_res['train_loss'].data] \
              + train_res['train_dice'].tolist()\
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

