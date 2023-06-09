__author__ = 'Elad Sofer <elad.g.sofer@gmail.com>'


import copy

import torch.nn as nn
import torch

from utills import m, H

from visualize_model import LandscapeWrapper
import numpy as np

from utills import generate_signal, plot_conv_rec_graph, BIM, plot_3d_surface, \
    plot_2d_surface, plot_1d_surface, plot_norm_graph, plot_observations

from utills import sig_amount, r_step, eps_min, eps_max, loss3d_res_steps

np.random.seed(0)

# ADMM configuration
step_size = 0.00005
max_iter = 10000
rho = 0.01
eps_threshold = 1e-3
lambda_ = 12.5


class ADMM(nn.Module, LandscapeWrapper):
    """
    Implements the Alternating Direction Method of Multipliers (ADMM) algorithm for sparse signal recovery.
    Args:
        H (torch.Tensor): Sensing matrix.
        mu (float): Solver parameter for updating u.
        lambda_ (float): Regularization parameter for L1-norm penalty.
        max_iter (int): Maximum number of iterations.
        eps (float): Convergence threshold.
        rho (float): Penalty parameter.

    Attributes:
        mu (float): Solver parameter for updating u.
        max_iter (int): Maximum number of iterations.
        eps (float): Convergence threshold.
        rho (float): Penalty parameter.
        lambda_ (float): Regularization parameter for L1-norm penalty.
        H (torch.Tensor): Sensing matrix.
        left_term (torch.Tensor): Left term used for updating s.
        s (torch.Tensor): Initial estimate of the sparse signal.
        u (torch.Tensor): Variable used in ADMM algorithm.
        v (torch.Tensor): Variable used in ADMM algorithm.
        model_params (nn.Parameter): Model parameters used for visualization.

    """

    def __init__(self, H, mu, lambda_, max_iter, eps, rho):
        super(ADMM, self).__init__()

        # Solver parameters
        self.mu = mu
        self.max_iter = max_iter
        self.eps = eps

        # ρ parameter
        self.rho = rho
        # λ parameter
        self.lambda_ = lambda_ 

        # Objective parameters
        self.H = H

        # left_term = (H^TH+2λI)^-1
        self.left_term = torch.linalg.inv(torch.matmul(self.H.T, self.H) + self.rho * torch.eye(self.H.shape[1]))

        # initial estimate
        self.s = torch.zeros((H.shape[1], 1))
        self.u = torch.zeros((H.shape[1], 1))
        self.v = torch.zeros((H.shape[1], 1))

    @staticmethod
    def shrinkage(x, beta):
        """
        Applies the shrinkage operator to the input tensor 'x' with a threshold of 'beta'.
        :param x: Input tensor.
        :param beta: Threshold value.
        :return: Resulting tensor after applying shrinkage.
        """
        return torch.mul(torch.sign(x), torch.max(torch.abs(x) - beta, torch.zeros((m, 1))))

    def forward(self, x):
        """
        Performs ISTA reconstruction on the input signal 'x'.
        :param x: Input signal to reconstruct. (torch.Tensor)

        :returns
        torch.Tensor: Reconstructed sparse signal.
        list: List of recovery errors at each iteration.
        """
        recovery_errors = []
        for k in range(self.max_iter):
            s_prev, v_prev, u_prev = self.s, self.v, self.u

            # Update s_k+1 = ((H^T)H+2λI)^−1(H^T x+2λ(vk−uk)).
            right_term = torch.matmul(H.T, x) + self.rho * (v_prev - u_prev)
            self.s = self.left_term @ right_term

            # Update vk+1 = prox_(1/2λϕ)(sk+1 + uk)
            self.v = self.shrinkage(self.s + u_prev, self.rho / (2 * self.lambda_))

            # Update uk+1 = uk + μ (sk+1 − vk+1).
            self.u = u_prev + self.mu * (self.s - self.v)

            # cease if convergence achieved
            if torch.sum(torch.abs(self.s - s_prev)) <= self.eps:  break

            # save recovery error
            recovery_errors.append(torch.sum((torch.matmul(self.H, self.s) - x) ** 2).item())
        return self.s, recovery_errors

    def set_model_visualization_params(self):
        """
        Sets the model parameters for visualization for the visualize_model module to operate.
        """
        self.model_params = nn.Parameter(self.s.clone().detach(), requires_grad=False)

    def loss_func(self, s, x_sig):
        """
        Computes the loss function given the estimated sparse signal 's' and its observation 'x_sig'.
        :param s: Estimated sparse signal.
        :param x_sig: observation signal x = Hs + w, where w is a Gaussian noise.
        :return: Loss value.
        """
        return 0.5 * torch.sum((torch.matmul(self.H, s) - x_sig) ** 2).item() + self.rho * s.norm(p=1).item()

    @staticmethod
    def copy(src_admm):
        """
        Copy an ADMM object.
        :param src_admm: the source object to copy from.
        :return: ADMM object, a copy of the model
        """
        x = ADMM(copy.deepcopy(src_admm.H), src_admm.mu, src_admm.lambda_, src_admm.max_iter, src_admm.eps,
                 src_admm.rho)
        x.s = src_admm.s.clone().detach()
        x.s.requires_grad = False
        x.set_model_visualization_params()
        return x

    @classmethod
    def create_ADMM(cls, H=H, step_size=step_size, rho=rho, max_iter=max_iter, eps_threshold=eps_threshold,
                    lambda_=lambda_):
        """
        Creates an instance of the ADMM class with the specified parameters.
        :param H: Sensing matrix.
        :param step_size: Solver parameter for updating u.
        :param max_iter: Maximum number of iterations.
        :param lambda_: Regularization parameter for L1-norm penalty.
        :param eps_threshold: Convergence threshold.
        :param rho: Penalty parameter.
        :return: ADMM object.
        """
        return cls(H, step_size, lambda_, max_iter, eps_threshold, rho)


def execute():
    """
    Perform a series of operations on generated signals:
    1. Generate 'c' (set to 100) signals of the form x_i = Hs + w, where w follows a Gaussian distribution.
    2. Perform ADMM reconstruction on each signal x to obtain s^*.
    3. Perform BIM adversarial attack with different epsilon values to obtain x_{adv}.
    4. Perform ADMM reconstruction on each signal x_{adv} to obtain s_{adv}.
    5. Aggregate the L2 norm ||s^* - s^*_{adv}|| for each signal and epsilon value.
    6. Plot the loss surfaces in various forms (3D, 2D, 1D) and other related graphs.
    """
    signals = []

    # ISTA_min_distances = np.load('stack/version1/matrices/ISTA_total_norm.npy')
    # ADMM_min_distances = np.load('stack/version1/matrices/ADMM_total_norm.npy')

    dist_total = np.zeros((sig_amount, r_step))
    radius_vec = np.linspace(eps_min, eps_max, r_step)

    # Generate signals
    for i in range(sig_amount):
        signals.append(generate_signal())

    ##########################################################

    for sig_idx, (x_original, s_original) in enumerate(signals):
        # ADMM without an attack reconstruction
        ADMM_t_model = ADMM.create_ADMM()
        s_gt, err_gt = ADMM_t_model(x_original.detach())
        print("#### ADMM signal {0} convergence: iterations: {1} ####".format(sig_idx, len(err_gt)))
        s_gt = s_gt.detach()

        for e_idx, attack_eps in enumerate(radius_vec):
            # print("Performing BIM to get Adversarial Perturbation - epsilon: {0}".format(r))

            ADMM_adv_model = ADMM.create_ADMM()

            adv_x, delta = BIM(ADMM_adv_model, x_original, s_original, eps=attack_eps)
            adv_x = adv_x.detach()

            s_attacked, err_attacked = ADMM_adv_model(adv_x)
            # print("Attacked-ISTA convergence: iterations: {0}".format(len(err_attacked)))

            dist_total[sig_idx, e_idx] = (s_gt - s_attacked).norm(2).item()

    # np.save('data/stack/version1/matrices/ADMM_total_norm.npy', dist_total)

    ##########################################################
    plot_norm_graph(radius_vec, dist_total.mean(axis=0), fname="NORM2_ADMM.pdf")

    # Presenting last iteration signal loss graphs for r=max_eps
    x = x_original.detach()

    plot_conv_rec_graph(s_attacked.detach().numpy(), s_gt.detach().numpy(), s_original,
                        err_attacked, err_gt,
                        fname='convergence_ADMM.pdf')

    # plot observations
    plot_observations(adv_x, x, fname="ADMM_observation.pdf")

    dir_one, dir_two = ADMM_t_model.get_grid_vectors(ADMM_t_model, ADMM_adv_model)

    # Plotting 1D
    gt_line = ADMM_t_model.linear_interpolation(model_start=ADMM_t_model, model_end=ADMM_adv_model, x_sig=x,
                                                deepcopy_model=True)
    adv_line = ADMM_t_model.linear_interpolation(model_start=ADMM_t_model, model_end=ADMM_adv_model, x_sig=adv_x,
                                                 deepcopy_model=True)

    plot_1d_surface(gt_line, adv_line, 'ADMM_1D_LOSS.pdf')

    # Plotting 2D & 3D
    Z_gt, Z_adv = ADMM_t_model.random_plane(gt_model=ADMM_t_model, adv_model=ADMM_adv_model,
                                            adv_x=adv_x, x=x, dir_one=dir_one, dir_two=dir_two, steps=loss3d_res_steps)

    # np.save('data/stack/version1/matrices/ADMM_Z_adv.npy', Z_adv)
    # np.save('data/stack/version1/matrices/ADMM_Z_gt.npy', Z_gt)

    plot_2d_surface(Z_gt, Z_adv, 'ADMM_2D_LOSS.pdf')

    plot_3d_surface(z_adv=Z_adv, z_gt=Z_gt, steps=loss3d_res_steps, fname='ADMM_3D_LOSS_SURFACE.pdf')


if __name__ == '__main__':
    execute()
