import argparse
import os
import shutil
import time
import random
import resnet
import numpy as np
import math
import copy
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.datasets as datasets
from opacus.utils import module_modification
from opacus import PrivacyEngine
from tqdm import tqdm
from resnet import BasicCNN

from gradient_utils import get_first_batch_data, prune_grad_percentage, copy_model, prune_grad_val, trunc_grad, \
    normalize_grad
from gradient_utils import model_mix, dot_product, recompute_bn_gradient, generate_mask, multiply_mask
from mix_data_utils import mixup_data, mixup_public_data, mixup_criterion

model_names = sorted(name for name in resnet.__dict__
                     if name.islower() and not name.startswith("__")
                     and name.startswith("resnet")
                     and callable(resnet.__dict__[name]))

# print("resnet20, method 0 L2, gap =0.15, randommix, grouping=3, clip_norm, 1 batch_size: 600, noise 0.004, start_rate 0.01")
# print("resnet20, comparison 0.0015 L_1 3 0.01 tau = 0 ")


dev = 0
device = torch.device('cuda:0')

batch_select = 366
true_batch = 3660 / batch_select
start_lr = 0.25
gap_rate = 0
num_epoch = 200
noise_scale = 0.0025


print_trunc_percentage = 1.0

use_mix = True  # use random mixz
use_norm = True  # normalize gradient
clip_type = 2.0
clip_norm = 8

use_group = False
small_batch = 1
small_batch_num = int(batch_select / small_batch)

print_detail_norm = False

use_merge = False
merge_epoch = 20
use_prune_after_norm = False
prune_percentage_2 = 0
use_public_mask = False
mask_percentage = 90

use_prune = False  # prune per sample gradient before adding them
prune_percentage = 0

use_trunc = False  # truncate per sample gradient before adding them
max_val = 1.2

use_expand = False  # generate multiple data samples from few samples
expand_batch_size = 20
use_public_expand = False  # combine few samples into multiple samples using public samples
public_batch_size = 20
use_precompute_bn = False  # replace bn gradient with precompute_bn
calc_bn_size = 50
use_SVHN = True
use_CIFAR10 = False
use_FMNIST = False
train_fc_only = False  # only train fully connected layer

if use_norm:
    print("Use Norm", use_norm, "Group", small_batch, "CLIP NORM", clip_norm, "Use Mix", use_mix, gap_rate, "Noise",
          noise_scale, "Num of Epochs",
          num_epoch, "Device", dev)

parser = argparse.ArgumentParser(description='Propert ResNets for CIFAR10 in pytorch')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet20',
                    choices=model_names,
                    help='model architecture: ' + ' | '.join(model_names) +
                         ' (default: resnet32)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=num_epoch, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=batch_select, type=int,
                    metavar='N', help='mini-batch size (default: 128)')
parser.add_argument('--lr', '--learning-rate', default=start_lr, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=2000, type=int,
                    metavar='N', help='print frequency (default: 50)')
parser.add_argument('--resume', default='', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
# parser.add_argument('--resume', default='save_temp/checkpointNew.th', type=str, metavar='PATH',
#                    help='path to latest checkpoint (default: none)')

parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
# parser.add_argument('--pretrained', dest='pretrained', action='store_true',  help='use pre-trained model')
parser.add_argument('--half', dest='half', action='store_true',
                    help='use half-precision(16-bit) ')
parser.add_argument('--save-dir', dest='save_dir',
                    help='The directory used to save the trained models',
                    default='save_temp', type=str)
parser.add_argument('--save-every', dest='save_every',
                    help='Saves checkpoints at every specified number of epochs',
                    type=int, default=10)
best_prec1 = 0


def main():
    global args, best_prec1, dev
    args = parser.parse_args()

    # Check the save_dir exists or not
    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)
    '''
    model = torch.hub.load('pytorch/vision:v0.10.0', 'wide_resnet50_2', pretrained=True)
    model2 = torch.hub.load('pytorch/vision:v0.10.0', 'wide_resnet50_2', pretrained=True)
    store_model = torch.hub.load('pytorch/vision:v0.10.0', 'wide_resnet50_2', pretrained=True)
    '''

    model = resnet.__dict__[args.arch]()
    model2 = resnet.__dict__[args.arch]()
    '''
    model = BasicCNN()
    model2 = BasicCNN()
    '''
    # model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', pretrained=False)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            temp_dict = model.state_dict()
            for name in temp_dict.keys():
                if ('bn' not in name or temp_dict[name].shape != checkpoint['state_dict'][name].shape):
                    checkpoint['state_dict'][name] = temp_dict[name]
            model.load_state_dict(checkpoint['state_dict'])
            model2.load_state_dict(checkpoint['state_dict'])
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    # model.cuda(device)
    # model2.cuda(device)
    model = module_modification.convert_batchnorm_modules(model).cuda(device)
    model2 = module_modification.convert_batchnorm_modules(model2).cuda(device)

    cudnn.benchmark = True

    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    if use_SVHN:
        train_loader = torch.utils.data.DataLoader(
            datasets.SVHN(root='./data', split='train', transform=transforms.Compose([
                transforms.RandomHorizontalFlip(),
                transforms.RandomCrop(32, 4),
                transforms.ToTensor(),
                normalize,
            ]), download=True),
            batch_size=args.batch_size, shuffle=True,
            num_workers=args.workers, pin_memory=True)

        val_loader = torch.utils.data.DataLoader(
            datasets.SVHN(root='./data', split='test', transform=transforms.Compose([
                transforms.ToTensor(),
                normalize,
            ]), download=True),
            batch_size=1000, shuffle=False,
            num_workers=args.workers, pin_memory=True)
    elif use_CIFAR10:
        train_loader = torch.utils.data.DataLoader(
            datasets.CIFAR10(root='./data', train=True, transform=transforms.Compose([
                transforms.RandomHorizontalFlip(),
                transforms.RandomCrop(32, 4),
                transforms.ToTensor(),
                normalize,
            ]), download=True),
            batch_size=batch_select, shuffle=True,
            num_workers=args.workers, pin_memory=True)

        val_loader = torch.utils.data.DataLoader(
            datasets.CIFAR10(root='./data', train=False, transform=transforms.Compose([
                transforms.ToTensor(),
                normalize,
            ])),
            batch_size=2000, shuffle=False,
            num_workers=args.workers, pin_memory=True)

    elif use_FMNIST:
        train_loader = torch.utils.data.DataLoader(
            datasets.FashionMNIST(root='./data', train=True, transform=transforms.Compose([
                transforms.RandomHorizontalFlip(),
                transforms.RandomCrop(32, 4),
                transforms.ToTensor(),
                normalize,
            ]), download=True),
            batch_size=batch_select, shuffle=True,
            num_workers=args.workers, pin_memory=True)

        val_loader = torch.utils.data.DataLoader(
            datasets.FashionMNIST(root='./data', train=False, transform=transforms.Compose([
                transforms.ToTensor(),
                normalize,
            ])),
            batch_size=2000, shuffle=False,
            num_workers=args.workers, pin_memory=True)

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda(device)
    # criterion = nn.MSELoss().cuda(device)

    if args.half:
        model.half()
        model2.half()
        criterion.half()

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)
    privacy_engine = PrivacyEngine(model, batch_size=batch_select, sample_size=len(train_loader.dataset),
                                   alphas=range(2, 32), noise_multiplier=noise_scale, max_grad_norm=clip_norm)
    privacy_engine.attach(optimizer)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[100,150], last_epoch=args.start_epoch - 1)
    lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.994, last_epoch=args.start_epoch - 1)

    '''
    optimizer2 = torch.optim.SGD(model2.parameters(), args.lr,
                                 momentum=args.momentum,
                                 weight_decay=args.weight_decay)
    privacy_engine2 = PrivacyEngine(model2, batch_size=batch_select, sample_size=len(train_loader.dataset),
                                    alphas=range(2, 32), noise_multiplier=noise_mul, max_grad_norm=clip_norm)
    privacy_engine2.attach(optimizer2)
    lr_scheduler2 = torch.optim.lr_scheduler.MultiStepLR(optimizer2, milestones=[30, 50],
                                                         last_epoch=args.start_epoch - 1)
    '''

    # lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.98, last_epoch=args.start_epoch - 1)

    if args.arch in ['resnet1202', 'resnet110']:
        # for resnet1202 original paper uses lr=0.01 for first 400 minibatches for warm-up
        # then switch back. In this setup it will correspond for first epoch.
        for param_group in optimizer.param_groups:
            param_group['lr'] = args.lr * 0.1
        for param_group2 in optimizer2.param_groups:
            param_group2['lr'] = args.lr * 0.1
        for param_group3 in optimizer3.param_groups:
            param_group3['lr'] = args.lr * 0.1

    if args.evaluate:
        validate(val_loader, model, criterion)
        validate(val_loader, model2, criterion)
        return

    norm_vec, var_vec, acc_vec = [], [], []
    for epoch in range(args.start_epoch, args.epochs):
        print(epoch)
        # train for one epoch
        print('current lr {:.5e}'.format(optimizer.param_groups[0]['lr']))

        global gap_rate
        
        if epoch < 50:
            gap_rate = 0.025
        elif epoch < 100:
            gap_rate = 0.025
        else:
            gap_rate = 0.0125


        # global noise_scale, clip_norm
        # if epoch < 40:
        # noise_scale = 0.015
        # clip_norm = 10
        # else:
        # noise_scale = 0.005
        # clip_norm = 8

        train(train_loader, model, model2, criterion, optimizer, epoch, norm_vec, var_vec)
        print("norm vector: ", norm_vec)
        print("variance vector: ", var_vec)
        lr_scheduler.step()

        # evaluate on validation set
        prec1 = validate(val_loader, model, criterion)
        acc_vec.append(prec1)

        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)

        if epoch > 0 and epoch % args.save_every == 0:
            save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
            }, is_best, filename=os.path.join(args.save_dir, 'SVHNcheckpoint.th'))

        save_checkpoint({
            'state_dict': model.state_dict(),
            'best_prec1': best_prec1,
        }, is_best, filename=os.path.join(args.save_dir, 'model.th'))
    print("final mean: ", norm_vec)
    print("final variance: ", var_vec)
    print("accuracy list: ", acc_vec)


def calc_persample_norm(model):
    sum_norms = torch.zeros(batch_select).cuda(device)
    for name, param in model.named_parameters():
        norms = param.grad_sample.view(len(param.grad_sample), -1).norm(2, -1)
        sum_norms += norms * norms
    return torch.sqrt(sum_norms)


def calc_persample_norm_list(grad_sample_list):
    for name in grad_sample_list:
        list_size = grad_sample_list[name].shape[0]
        break
    sum_norms = torch.zeros(list_size).cuda(device)
    for name in grad_sample_list:
        norms = grad_sample_list[name].view(len(grad_sample_list[name]), -1).norm(2, -1)
        sum_norms += norms * norms
    return torch.sqrt(sum_norms)


def calc_real_norm(model):
    sum_norms = torch.tensor(0.0).cuda(device)
    for name, param in model.named_parameters():
        norm_val = torch.sum(param.grad_sample, 0).norm(2)
        sum_norms += norm_val * norm_val
    return torch.sqrt(sum_norms)


def grad_norm(model):
    sum_norm = torch.tensor(0.0).cuda(device)
    for name, param in model.named_parameters():
        norm_val = param.grad.norm(2)
        sum_norm += norm_val * norm_val
    sum_norm = torch.sqrt(sum_norm)
    if sum_norm > torch.tensor(clip_norm):
        for name, param in model.named_parameters():
            param.grad /= sum_norm / clip_norm


def print_var_mean(model):
    mean_norm = torch.tensor(0.0).cuda(device)
    var_norm = torch.tensor(0.0).cuda(device)
    for name, param in model.named_parameters():
        mean_grad = torch.sum(param.grad_sample, 0).unsqueeze(0) / param.grad_sample.shape[0]
        # print(mean_grad.shape, param.grad_sample.shape)
        mean_norm += mean_grad.norm(2) ** 2

        var_grad = param.grad_sample - mean_grad.expand(param.grad_sample.shape)
        var_norm += (var_grad.norm(2) ** 2) / param.grad_sample.shape[0]
    return (torch.sqrt(mean_norm).item(), torch.sqrt(var_norm).item())


def model_mix(model, model2, tau):
    temp_dict = copy.deepcopy(model.state_dict())
    temp_dict2 = copy.deepcopy(model2.state_dict())
    for name, param in model.named_parameters():
        if "bn" in name or name not in temp_dict.keys():
            continue
        dictShape = temp_dict[name].shape
        gap_opr = torch.abs(temp_dict[name] - temp_dict2[name])
        if torch.min(gap_opr) < tau:
            gap_opr = torch.clamp(gap_opr, min=0, max=tau)
            gap_opr = torch.add(-gap_opr, tau)
            sign_opr = torch.sign(temp_dict[name] - temp_dict2[name])
            temp_dict[name] += gap_opr * sign_opr / 2
            temp_dict2[name] -= gap_opr * sign_opr / 2

        oness = torch.ones(dictShape).cuda(device)
        alpha1 = torch.rand(dictShape).cuda(device)
        temp_dict[name] = alpha1 * temp_dict[name] + (oness - alpha1) * temp_dict2[name]
    model.load_state_dict(temp_dict)


def model_mix2(model, model2, tau, mask={}):
    temp_dict = model.state_dict()
    temp_dict2 = model2.state_dict()
    for name, param in model.named_parameters():
        if "bn" in name or name not in temp_dict.keys():
            continue
        dictShape = temp_dict[name].shape
        if name not in mask:
            temp_mask = torch.ones(dictShape).cuda(device)
        else:
            temp_mask = mask[name]
        gap_opr = torch.abs(temp_dict[name] - temp_dict2[name])
        if torch.min(gap_opr) < tau:
            gap_opr = torch.clamp(gap_opr, min=0, max=tau)
            gap_opr = torch.add(-gap_opr, tau)
            sign_opr = torch.sign(temp_dict[name] - temp_dict2[name]) * temp_mask
            temp_dict[name] += gap_opr * sign_opr / 2
            temp_dict2[name] -= gap_opr * sign_opr / 2

        oness = torch.ones(dictShape).cuda(device)
        alpha1 = torch.rand(dictShape).cuda(device)
        alpha1 = temp_mask * alpha1 + (oness - temp_mask) * oness / 2
        temp_dict[name] = alpha1 * temp_dict[name] + (oness - alpha1) * temp_dict2[name]
    model.load_state_dict(temp_dict)


def train(train_loader, model, model2, criterion, optimizer, epoch, norm_vec, var_vec):
    """
        Run one train epoch
    """
    # if epoch < 30:
    #    use_mask = False
    # else:
    #    use_mask = True

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to train mode
    model.train()
    model2.train()

    end = time.time()
    lr_temp = optimizer.param_groups[0]['lr']
    tau = lr_temp * gap_rate
    store_grad = {}
    loss_val = 0
    norms_list = []

    # loss_val, norm_val, norm_times = 0, 0, 0
    for i, (input, target) in enumerate(train_loader):
        if len(target) < batch_select:
            print("breaking!")
            break
        temp_model = model
        temp_model2 = model2
        temp_optimizer = optimizer

        '''
        if i % 2 == 0:
            temp_model = model
            temp_model2 = model2
            temp_optimizer = optimizer
        else:
            temp_model = model2
            temp_model2 = model
            temp_optimizer = optimizer2
        '''

        # measure data loading time
        data_time.update(time.time() - end)

        target = target.cuda(device)
        input_var = input.cuda(device)
        target_var = target

        # temp_model.eval()
        output = temp_model(input_var)
        loss = criterion(output, target_var)
        temp_optimizer.zero_grad()
        loss.backward()

        if (i + 1) % true_batch == 0:
            # assert(len(norms_list) == true_batch * batch_select)
            average_norm, var_norm = print_var_mean(temp_model)
            norm_vec.append(average_norm)
            var_vec.append(var_norm)
            # norm_val += average_norm.item()
            # norm_times += 1

        if print_detail_norm and i > 0 and i % (args.print_freq * 2) == 0:
            print("per sample mean: ", average_norm)
            print("per sample median: ", sum_norms.median())
            print("per sample max: ", sum_norms.max())
            print("true grad norm: ", calc_real_norm(temp_model) / batch_select)

        grad_sample_list = {}
        if use_group:
            multiply_matrix = torch.zeros(small_batch_num, batch_select).cuda(device)
            for j in range(batch_select):
                multiply_matrix[int(j / small_batch)][j] = 1 / torch.tensor(small_batch)
            for name, param in temp_model.named_parameters():
                grad_sample_list[name] = torch.einsum('ij, j...->i...', multiply_matrix, param.grad_sample)
        else:
            for name, param in temp_model.named_parameters():
                grad_sample_list[name] = param.grad_sample

        if use_norm:
            sum_norms = calc_persample_norm_list(grad_sample_list)
            sum_norms = torch.clamp(sum_norms, min=clip_norm)
            clip_factor = clip_norm / sum_norms

            for name, param in temp_model.named_parameters():
                if name not in store_grad:
                    store_grad[name] = torch.einsum("i,i...", clip_factor, grad_sample_list[name]) / sum_norms.shape[0]
                else:
                    store_grad[name] += torch.einsum("i,i...", clip_factor, grad_sample_list[name]) / sum_norms.shape[0]
        else:
            for name, param in temp_model.named_parameters():
                list_size = grad_sample_list[name].shape[0]
                if name not in store_grad:
                    store_grad[name] = grad_sample_list[name].sum(0).squeeze() / list_size
                else:
                    store_grad[name] += grad_sample_list[name].sum(0).squeeze() / list_size

        if (i + 1) % true_batch == 0:
            if use_mix:
                model_mix(temp_model, temp_model2, tau)

            for name, param in temp_model.named_parameters():
                if clip_type == 2.0:
                    noise = torch.randn(param.grad.shape).cuda(device)
                if clip_type == 1.0:
                    laplace_dist = torch.distributions.laplace.Laplace(0, 1)
                    noise = laplace_dist.sample(param.grad.shape).cuda(device)
                param.grad = store_grad[name] / true_batch + noise * noise_scale

            temp_dict = copy.deepcopy(temp_model.state_dict())
            temp_model2.load_state_dict(temp_dict)
            # temp_optimizer.step()
            store_grad = {}
            temp_optimizer.original_step()

        output = output.float()
        loss = loss.float()
        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        if i > 0 and i % (args.print_freq) == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.avg:.4f}\t'
                  'Prec@1 {top1.avg:.3f}'.format(
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss=losses, top1=top1))
    # norm_vec.append(norm_val / norm_times)


def validate(val_loader, model, criterion):
    """
    Run evaluation
    """
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    model.eval()

    end = time.time()
    with torch.no_grad():
        for i, (input, target) in enumerate(val_loader):
            target = target.cuda(device)
            input_var = input.cuda(device)
            target_var = target.cuda(device)

            if args.half:
                input_var = input_var.half()

            # compute output
            output = model(input_var)
            loss = criterion(output, target_var)

            output = output.float()
            loss = loss.float()

            # measure accuracy and record loss
            prec1 = accuracy(output.data, target)[0]
            losses.update(loss.item(), input.size(0))
            top1.update(prec1.item(), input.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                print('Test: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                    i, len(val_loader), batch_time=batch_time, loss=losses,
                    top1=top1))

    print(' * Prec@1 {top1.avg:.3f}'
          .format(top1=top1))

    return top1.avg


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    """
    Save the training model
    """
    torch.save(state, filename)


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == '__main__':
    main()