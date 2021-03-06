import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader, ConcatDataset
import tensorboardX as tb
from torchvision import transforms
import argparse
import numpy as np
from tensorboardX import SummaryWriter
import uuid as uid
from template import TemplateModel
from model import Stage2Model
from preprocess import Stage2ToPILImage, Stage2_ToTensor, OldStage2_ToPILImage, OldStage2Resize, OldStage2ToTensor
from dataset import PartsDataset
from data_augmentation import Stage2Augmentation
from prefetch_generator import BackgroundGenerator
from tqdm import tqdm

import torchvision
import os

uuid = str(uid.uuid1())[0:8]
print(uuid)
parser = argparse.ArgumentParser()
parser.add_argument("--batch_size", default=16, type=int, help="Batch size to use during training.")
parser.add_argument("--display_freq", default=10, type=int, help="Display frequency")
parser.add_argument("--pretrain", default=False, type=bool, help="True or False, Load pretrain parmeters")
parser.add_argument("--optim", default='Adam', type=str, help="Adam or SGD")
parser.add_argument("--lr0", default=0.0025, type=float, help="Learning rate for optimizer")
parser.add_argument("--lr1", default=0.0025, type=float, help="Learning rate for optimizer")
parser.add_argument("--lr2", default=0.0025, type=float, help="Learning rate for optimizer")
parser.add_argument("--lr3", default=0.0025, type=float, help="Learning rate for optimizer")
parser.add_argument("--datamore", default=0, type=int, help="enable data augmentation")
parser.add_argument("--momentum", default=0.9, type=float, help="valid when SGD ")
parser.add_argument("--weight_decay", default=0.001, type=float, help="valid when SGD ")
parser.add_argument("--cuda", default=8, type=int, help="Choose GPU with cuda number")
parser.add_argument("--epochs", default=25, type=int, help="Number of epochs to train")
parser.add_argument("--eval_per_epoch", default=1, type=int, help="eval_per_epoch ")
args = parser.parse_args()
print(args)

# Dataset Read_in Part
root_dir = "/data1/yinzi/datas"
parts_root_dir = "/home/yinzi/data3/recroped_parts"

txt_file_names = {
    'train': "exemplars.txt",
    'val': "tuning.txt"
}

transforms_list = {
    'train':
        transforms.Compose([
            Stage2ToPILImage(),
            Stage2_ToTensor()
        ]),
    'val':
        transforms.Compose([
            Stage2ToPILImage(),
            Stage2_ToTensor()
        ])
}


class DataLoaderX(DataLoader):
    def __iter__(self):
        return BackgroundGenerator(super().__iter__())


# Data Augmentation
stage2_augmentation = Stage2Augmentation(dataset=PartsDataset,
                                         txt_file=txt_file_names,
                                         root_dir=parts_root_dir
                                         )
enhaced_stage2_datasets = stage2_augmentation.get_dataset()
# DataLoader
Dataset = {x: PartsDataset(txt_file=txt_file_names[x],
                           root_dir=parts_root_dir,
                           transform=transforms_list[x]
                           )
           for x in ['train', 'val']
           }


if args.datamore == 0:
    dataloader = {x: DataLoaderX(Dataset[x], batch_size=args.batch_size,
                                 shuffle=True, num_workers=4)
                  for x in ['train', 'val']
                  }

elif args.datamore == 1:
    dataloader = {x: DataLoaderX(enhaced_stage2_datasets[x], batch_size=args.batch_size,
                                 shuffle=True, num_workers=4)
                  for x in ['train', 'val']
                  }


class TrainModel(TemplateModel):

    def __init__(self, argus=args):
        super(TrainModel, self).__init__()
        self.train_logger = None
        self.eval_logger = None
        self.args = argus

        # ============== neccessary ===============
        self.writer = SummaryWriter('log')
        self.step = 0
        self.epoch = 0
        self.best_error = float('Inf')

        self.device = torch.device("cuda:%d" % self.args.cuda if torch.cuda.is_available() else "cpu")

        self.model = Stage2Model().to(self.device)
        if self.args.pretrain:
            path = os.path.join("/home/yinzi/data4/new_train/checkpoints_C/02a38440", "best.pth.tar")
            self.load_state(path, map_location=self.device)
            self.epoch = 0
            self.step = 0

        lr = [self.args.lr0, self.args.lr1, self.args.lr2, self.args.lr3]
        if self.args.optim == 'Adam':
            self.optimizer = [optim.Adam(self.model.model[i].parameters(), lr[i])
                              for i in range(4)]
        elif self.args.optim == 'SGD':
            self.optimizer = [optim.SGD(self.model.model[i].parameters(), lr[i], momentum=self.args.momentum,
                                        weight_decay=self.args.weight_decay)
                              for i in range(4)]
        self.criterion = nn.CrossEntropyLoss()
        self.metric = nn.CrossEntropyLoss()
        self.scheduler = [optim.lr_scheduler.StepLR(self.optimizer[i], step_size=5, gamma=0.5)
                          for i in range(4)]

        self.train_loader = dataloader['train']
        self.eval_loader = dataloader['val']

        self.ckpt_dir = "checkpoints_C/%s" % uuid
        self.display_freq = self.args.display_freq
        # call it to check all members have been intiated
        self.check_init()

    def train_loss(self, batch):
        parts = batch['image'].to(self.device)
        parts_mask = batch['labels'].to(self.device)
        N = parts.shape[0]
        assert parts.shape == (N, 6, 3, 81, 81)
        assert parts_mask.shape == (N, 6, 81, 81)
        # for i in range(6):
        #     mask_grid = torchvision.utils.make_grid(parts_mask[:, i:i+1])
        #     self.writer.add_image("parts_mask_gt", mask_grid[0], global_step=self.step, dataformats='HW')
        pred = self.model(parts)
        loss = []
        for i in range(6):
            loss.append(self.criterion(pred[i], parts_mask[:, i].long()))
        loss = torch.stack(loss)
        return loss, None

    def eval_error(self):
        loss_list = []
        for batch in tqdm(self.eval_loader):
            parts = batch['image'].to(self.device)
            parts_mask = batch['labels'].to(self.device)
            N = parts.shape[0]

            assert parts.shape == (N, 6, 3, 81, 81)
            assert parts_mask.shape == (N, 6, 81, 81)

            pred = self.model(parts)

            loss = []
            for i in range(6):
                loss.append(self.criterion(pred[i], parts_mask[:, i].long()))
            loss = torch.stack(loss)
            loss_list.append(torch.sum(loss).item())
        return np.mean(loss_list), None

    def save_state(self, fname, optim=True):
        state = {}

        if isinstance(self.model, torch.nn.DataParallel):
            state['model2'] = self.model.module.state_dict()
        else:
            state['model2'] = self.model.state_dict()

        if optim:
            for i in range(4):
                state['optimizer%d' % i] = self.optimizer[i].state_dict()
        state['step'] = self.step
        state['epoch'] = self.epoch
        state['best_error'] = self.best_error
        torch.save(state, fname)
        print('save model at {}'.format(fname))

    def load_state(self, fname, optim=True, map_location=None):
        state = torch.load(fname, map_location=map_location)

        if isinstance(self.model, torch.nn.DataParallel):
            self.model.module.load_state_dict(state['model2'])
        else:
            self.model.load_state_dict(state['model2'])
        if optim and 'optimizer' in state:
            for i in range(4):
                self.optimizer[i].load_state_dict(state['optimizer%d' % i])
        self.step = state['step']
        self.epoch = state['epoch']
        self.best_error = state['best_error']
        print('load model from {}'.format(fname))

    def train(self):
        self.model.train()
        self.epoch += 1
        for batch in tqdm(self.train_loader):
            self.step += 1
            for i in range(4):
                self.optimizer[i].zero_grad()
            loss, others = self.train_loss(batch)
            loss.backward(torch.ones(6, device=self.device, requires_grad=False))
            for i in range(4):
                self.optimizer[i].step()

            if self.step % self.display_freq == 0:
                self.writer.add_scalar('loss_all_%s' % uuid, torch.mean(loss).item(), self.step)
                print('epoch {}\tstep {}\t\n'
                      'loss_0 {:3}\tloss_1 {:3}\tloss_2 {:3}\t'
                      'loss_3 {:3}\tloss_4 {:3}\tloss_5 {:3}\t'
                      '\nloss_all {:.3}'.format(self.epoch, self.step, loss[0], loss[1], loss[2],
                                                loss[3], loss[4], loss[5],
                                                torch.sum(loss).item()))
                if self.train_logger:
                    self.train_logger(self.writer, others)

    def eval(self):
        self.model.eval()
        error, others = self.eval_error()

        if error < self.best_error:
            self.best_error = error
            self.save_state(os.path.join(self.ckpt_dir, 'best.pth.tar'), False)
        self.save_state(os.path.join(self.ckpt_dir, '{}.pth.tar'.format(self.epoch)))
        self.writer.add_scalar('error_all_%s' % uuid, error, self.epoch)
        print('epoch {}\terror_all {:.3}\tbest_error_all {:.3}'.format(self.epoch, error, self.best_error))

        if self.eval_logger:
            self.eval_logger(self.writer, others)

        return error


class TrainModel_accu(TrainModel):
    def eval(self):
        self.model.eval()
        accu, mean_error = self.eval_accu()
        mean_accu = np.mean(accu)

        if mean_accu > self.best_accu:
            self.best_accu = mean_accu
            self.save_state(os.path.join(self.ckpt_dir, 'best.pth.tar'), False)
        self.save_state(os.path.join(self.ckpt_dir, '{}.pth.tar'.format(self.epoch)))
        self.writer.add_scalar(f'accu_val_{uuid}', mean_accu, self.epoch)

        print('epoch {}\t mean_error {:.3}\t '
              'lbrow_accu {:.3}\trbrow_accu {:.3}\t'
              'leye_accu {:.3}\treye_accu {:.3}\t'
              'nose_accu {:.3}\tmouth_accu {:.3}\t'
              'mean_accu {:3}\tbest_accu {:.3}'.format(self.epoch, mean_error,
                                                       accu[0], accu[1],
                                                       accu[2], accu[3],
                                                       accu[4], accu[5],
                                                       mean_accu, self.best_accu))
        if self.eval_logger:
            self.eval_logger(self.writer, None)

    def eval_accu(self):
        label_channels = [2, 2, 2, 2, 2, 4]
        loss_list = []
        hist_list = {0: [],
                     1: [],
                     2: [],
                     3: [],
                     4: [],
                     5: []}
        for batch in tqdm(self.eval_loader):
            parts = batch['image'].to(self.device)
            parts_mask = batch['labels'].to(self.device)
            N = parts.shape[0]

            assert parts.shape == (N, 6, 3, 81, 81)
            assert parts_mask.shape == (N, 6, 81, 81)

            pred = self.model(parts)

            loss = []
            for i in range(6):
                pred_arg = pred[i].argmax(dim=1, keepdim=False).cpu().numpy()
                loss.append(self.criterion(pred[i], parts_mask[:, i].long()))
                hist_list[i].append(
                    self.fast_histogram(pred_arg,
                                        parts_mask[:, i].long().cpu().numpy(),
                                        label_channels[i], label_channels[i])
                )
            loss = torch.stack(loss)
            loss_list.append(torch.sum(loss).item())

        mean_error = np.mean(loss_list)
        F1_list = []
        for i in range(6):
            hist_sum = np.sum(np.stack(hist_list[i], axis=0), axis=0)
            A = hist_sum[1:label_channels[i], :].sum()
            B = hist_sum[:, 1:label_channels[i]].sum()
            intersected = hist_sum[1:label_channels[i], :][:, 1:label_channels[i]].sum()
            F1_list.append(2 * intersected / (A + B))

        return F1_list, mean_error

    def fast_histogram(self, a, b, na, nb):
        '''
        fast histogram calculation
        ---
        * a, b: non negative label ids, a.shape == b.shape, a in [0, ... na-1], b in [0, ..., nb-1]
        '''
        assert a.shape == b.shape
        assert np.all((a >= 0) & (a < na) & (b >= 0) & (b < nb))
        # k = (a >= 0) & (a < na) & (b >= 0) & (b < nb)
        hist = np.bincount(
            nb * a.reshape([-1]).astype(int) + b.reshape([-1]).astype(int),
            minlength=na * nb).reshape(na, nb)
        assert np.sum(hist) == a.size
        return hist


def start_train():
    train = TrainModel_accu(args)

    for epoch in range(args.epochs):
        train.train()
        for i in range(4):
            train.scheduler[i].step(epoch)
        if (epoch + 1) % args.eval_per_epoch == 0:
            train.eval()

    print('Done!!!')


if __name__ == "__main__":
    start_train()
