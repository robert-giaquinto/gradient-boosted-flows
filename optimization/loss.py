import numpy as np
import torch
import torch.nn as nn
from utils.distributions import log_normal_diag, log_normal_standard, log_bernoulli
import torch.nn.functional as F
from utils.utilities import safe_log


def binary_neg_elbo(x_recon, x, z_mu, z_var, z_0, z_k, ldj, beta=1.0):
    """
    Computes the binary loss function while summing over batch dimension, not averaged!
    :param x_recon: shape: (batch_size, num_channels, pixel_width, pixel_height), bernoulli parameters p(x=1)
    :param x: shape (batchsize, num_channels, pixel_width, pixel_height), pixel values rescaled between [0, 1].
    :param z_mu: mean of z_0
    :param z_var: variance of z_0
    :param z_0: first stochastic latent variable
    :param z_k: last stochastic latent variable
    :param ldj: log det jacobian
    :param beta: beta for kl loss
    :return: loss, ce, kl
    """
    # - N E_q0 [ ln p(x|z_k) ]
    reconstruction_function = nn.BCELoss(reduction='sum')
    recon_loss = reconstruction_function(x_recon, x)

    # ln p(z_k)  (not averaged)
    log_p_zk = log_normal_standard(z_k, dim=1)
    # ln q(z_0)  (not averaged)
    log_q_z0 = log_normal_diag(z_0, mean=z_mu, log_var=safe_log(z_var), dim=1)
    # N E_q0[ ln q(z_0) - ln p(z_k) ]
    summed_logs = torch.sum(log_q_z0 - log_p_zk)

    # sum over batches
    summed_ldj = torch.sum(ldj)

    # ldj = N E_q_z0[\sum_k log |det dz_k/dz_k-1| ]
    kl = summed_logs - summed_ldj
    loss = recon_loss + beta * kl

    batch_size = x.size(0)
    loss = loss / float(batch_size)
    recon_loss = recon_loss / float(batch_size)
    kl = kl / float(batch_size)

    return loss, recon_loss, kl


def boosted_binary_neg_elbo(x_recon, x, z_mu, z_var, z_g, g_ldj, z_G, G_ldj, regularization_rate, first_component, beta=1.0):
    reconstruction_function = nn.BCELoss(reduction='sum')
    recon_loss = reconstruction_function(x_recon, x)

    # prior: ln p(z_k)  (not averaged)
    log_p_zk = torch.sum(log_normal_standard(z_g[-1], dim=1))

    # entropy loss w.r.t. to new component terms (not averaged)
    # N E_g[ ln g(z | x) ]  (not averaged)
    log_g_base = log_normal_diag(z_g[0], mean=z_mu, log_var=safe_log(z_var), dim=1)
    log_g_z = log_g_base - g_ldj

    if first_component or (z_G is None and G_ldj is None):
        # train the first component just like a standard VAE + Normalizing Flow
        # or if we sampled from all components to alleviate decoder shock
        entropy = torch.sum(log_g_z)
        log_G_z = torch.zeros_like(entropy)
        log_ratio = torch.zeros_like(entropy).detach()
    else:
        # all other components are trained using the boosted loss
        # loss w.r.t. fixed component terms:
        log_G_base = log_normal_diag(z_G[0], mean=z_mu, log_var=safe_log(z_var), dim=1)
        # limit log likelihoods to a small number for numerical stability
        log_G_z = torch.sum(torch.max(log_G_base - G_ldj, torch.ones_like(G_ldj) * -10.0))
        #log_ratio = torch.sum(regularization_rate * log_g_z.data - log_G_z.data).detach()
        log_ratio = torch.sum(log_G_z.data - log_g_z.data).detach()
        entropy = torch.sum(regularization_rate * log_g_z)

    loss = recon_loss + log_G_z + beta*(entropy - log_p_zk)

    batch_size = float(x.size(0))
    loss = loss / batch_size
    recon_loss = recon_loss / batch_size
    log_G_z = log_G_z / batch_size
    log_p_zk = -1.0 * log_p_zk / batch_size
    entropy = entropy / batch_size
    log_ratio = log_ratio / batch_size

    return loss, recon_loss, log_G_z, log_p_zk, entropy, log_ratio


def multinomial_neg_elbo(x_logit, x, z_mu, z_var, z_0, z_k, ldj, args, beta=1.):
    """
    Computes the cross entropy loss function while summing over batch dimension, not averaged!
    :param x_logit: shape: (batch_size, num_classes * num_channels, pixel_width, pixel_height), real valued logits
    :param x: shape (batchsize, num_channels, pixel_width, pixel_height), pixel values rescaled between [0, 1].
    :param z_mu: mean of z_0
    :param z_var: variance of z_0
    :param z_0: first stochastic latent variable
    :param z_k: last stochastic latent variable
    :param ldj: log det jacobian
    :param args: global parameter settings
    :param beta: beta for kl loss
    :return: loss, ce, kl
    """
    num_classes = 256
    batch_size = x.size(0)

    x_logit = x_logit.view(batch_size, num_classes, args.input_size[0], args.input_size[1], args.input_size[2])

    # make integer class labels
    target = (x * (num_classes-1)).long()

    # - N E_q0 [ ln p(x|z_k) ]
    # sums over batch dimension (and feature dimension)
    ce = cross_entropy(input=x_logit, target=target, reduction='sum')

    # ln p(z_k)  (not averaged)
    log_p_zk = log_normal_standard(z_k, dim=1)
    # ln q(z_0)  (not averaged)
    log_q_z0 = log_normal_diag(z_0, mean=z_mu, log_var=safe_log(z_var), dim=1)
    # N E_q0[ ln q(z_0) - ln p(z_k) ]
    summed_logs = torch.sum(log_q_z0 - log_p_zk)

    # sum over batches
    summed_ldj = torch.sum(ldj)

    # ldj = N E_q_z0[\sum_k log |det dz_k/dz_k-1| ]
    kl = (summed_logs - summed_ldj)
    loss = ce + beta * kl

    loss = loss / float(batch_size)
    ce = ce / float(batch_size)
    kl = kl / float(batch_size)

    return loss, ce, kl


def binary_loss_array(x_recon, x, z_mu, z_var, z_0, z_k, ldj, beta=1.):
    """
    Computes the binary loss without averaging or summing over the batch dimension.
    """
    batch_size = x.size(0)

    # if not summed over batch_dimension
    if len(ldj.size()) > 1:
        ldj = ldj.view(ldj.size(0), -1).sum(-1)

    # TODO: upgrade to newest pytorch version on master branch, there the nn.BCELoss comes with the option
    # reduce, which when set to False, does no sum over batch dimension.
    bce = - log_bernoulli(x.view(batch_size, -1), x_recon.view(batch_size, -1), dim=1)
    # ln p(z_k)  (not averaged)
    log_p_zk = log_normal_standard(z_k, dim=1)
    # ln q(z_0)  (not averaged)
    log_q_z0 = log_normal_diag(z_0, mean=z_mu, log_var=safe_log(z_var), dim=1)
    #  ln q(z_0) - ln p(z_k) ]
    logs = log_q_z0 - log_p_zk

    loss = bce + beta * (logs - ldj)
    return loss


def multinomial_loss_array(x_logit, x, z_mu, z_var, z_0, z_k, ldj, args, beta=1.):
    """
    Computes the discritezed logistic loss without averaging or summing over the batch dimension.
    """

    num_classes = 256
    batch_size = x.size(0)

    x_logit = x_logit.view(batch_size, num_classes, args.input_size[0], args.input_size[1], args.input_size[2])

    # make integer class labels
    target = (x * (num_classes - 1)).long()

    # - N E_q0 [ ln p(x|z_k) ]
    # computes cross entropy over all dimensions separately:
    # ce = cross_entropy(x_logit, target, reduction='none')
    ce_loss_function = nn.CrossEntropyLoss(reduction='none')
    ce = ce_loss_function(x_logit, target, reduction='none')
    # sum over feature dimension
    ce = ce.view(batch_size, -1).sum(dim=1)

    # ln p(z_k)  (not averaged)
    log_p_zk = log_normal_standard(z_k.view(batch_size, -1), dim=1)
    # ln q(z_0)  (not averaged)
    log_q_z0 = log_normal_diag(z_0.view(batch_size, -1), mean=z_mu.view(batch_size, -1),
                               log_var=safe_log(z_var).view(batch_size, -1), dim=1)

    #  ln q(z_0) - ln p(z_k) ]
    logs = log_q_z0 - log_p_zk

    loss = ce + beta * (logs - ldj)

    return loss


def cross_entropy(x, target, reduction='none'):
    """
        input: Variable :math:`(N, C)` where `C = number of classes`
        target: Variable :math:`(N)` where each value is
            `0 <= targets[i] <= C-1`
    """
    log_softmax_x = F.log_softmax(x, 1)
    n = log_softmax_x.size(0)
    c = log_softmax_x.size(1)
    out_size = (n,) + log_softmax_x.size()[2:]
    log_softmax_x = log_softmax_x.contiguous().view(n, c, 1, -1)
    target = target.contiguous().view(n, 1, -1)

    nll_loss_function = nn.NLLLoss(reduction=reduction)
    loss = nll_loss_function(input=log_softmax_x, target=target)
    return loss


def calculate_boosted_loss(x_recon, x, z_mu, z_var, z_g, g_ldj, z_G, G_ldj, args, first_component, beta=1.0):

    if args.input_type == "binary":
        loss, recon, log_G, log_p, entropy, log_ratio = boosted_binary_neg_elbo(
            x_recon, x, z_mu, z_var, z_g, g_ldj, z_G, G_ldj, args.regularization_rate, first_component, beta)

    else:
        ValueError(f"Invalid inpt type for calculate_boosted_loss: {args.input_type}")

    return loss, recon, log_G, log_p, entropy, log_ratio
        

def calculate_loss(x_recon, x, z_mu, z_var, z_0, z_k, ldj, args, beta=1.):
    """
    Picks the correct loss depending on the input type.
    """

    if args.input_type == 'binary':
        loss, rec, kl = binary_neg_elbo(x_recon, x, z_mu, z_var, z_0, z_k, ldj, beta=beta)
    elif args.input_type == 'multinomial':
        loss, rec, kl = multinomial_neg_elbo(x_recon, x, z_mu, z_var, z_0, z_k, ldj, args, beta=beta)
    else:
        raise ValueError('Invalid input type for calculate loss: %s.' % args.input_type)

    return loss, rec, kl


def calculate_loss_array(x_recon, x, z_mu, z_var, z_0, z_k, ldj, args):
    """
    Picks the correct loss depending on the input type.
    """

    if args.input_type == 'binary':
        loss = binary_loss_array(x_recon, x, z_mu, z_var, z_0, z_k, ldj)
    elif args.input_type == 'multinomial':
        loss = multinomial_loss_array(x_recon, x, z_mu, z_var, z_0, z_k, ldj, args)

    else:
        raise ValueError('Invalid input type for calculate loss: %s.' % args.input_type)

    return loss




