"""
Created on Feb 13 2015
@author Wei Wang

Implement the base class for IOHMM
The implementation is based on:
    - Input-Output HMM's for Sequence processing
    - scikit-learn HMM implementation
"""
from __future__ import division
__author__ = 'Wei Wang'
__email__ = 'tskatom@vt.edu'

from functools import partial
import numpy as np
from util import extmath
from scipy.optimize import minimize
import math
from scipy.sparse import dia_matrix
import sys

EPS = np.finfo(float).eps


class _BaseIOHMM():
    """
    Input output Hidden Markov model base class
    Representation of a IO-HMM model probability distribution.
    This class allows for sampling from, evaluation and maximum-likelihood
    estimation of the parameters of a HMM

    Attributes
    ----------
    n_components : int
        Numbers of the states in the model

    ins : list
        list of array-like observation sequences, each of which has shape
                (n_i, n_features), which n_i is the length of the i_th observation

    startprob : array, shape ('n_components')
        Initiate state occupation distribution
        
    algorithm : string, one of the decoder_algorithms
        Decoder Algorithms

    random_state : RandomState or an int seed (0 by default)
        A random number generator instance

    n_iter : int, optional
        Number of iterations to perform

    thresh : float, optional
        Convergence threshold

    params: string, optional
        Controls which parameters are updated in the training process.

    init_params: string, optional
        Controls which parameters are initiated prior to training

    Interfaces
    ----------
    This class implements the public interface for all IOHMMs that derived from it,
    including all of the machinery for the forward-backward Viterbi algorithm. Subclass
    only need to implement _generate_sample_from_state(), _compute_likelihood(),
    _init(), _initiatelize_sufficient_statistics() and _do_mstep()
    """

    def __init__(self, n_components, ins, obs, start_prob=None, algorithm="viterbi",
                 random_state=None, n_iter=20, thresh=1e-8):

        self.n_components = n_components
        self.ins = ins
        self.obs = obs
        self.n_iter = n_iter
        self.thresh = thresh
        self.start_prob = start_prob
        self.algorithm = algorithm
        self.random_state = random_state
        self.gamma = 3

        # add dummy parameter to the input matrix
        for p in range(len(self.ins)):
            # normalize the input data
            x = np.matrix(self.ins[p])
            mean_x = np.mean(x, axis=0)
            std_x = np.std(x, axis=0 )
            self.ins[p] = (x - mean_x)/std_x

            dummy = np.matrix(np.ones(len(self.ins[p]))).T
            self.ins[p] = np.hstack([dummy, self.ins[p]])
            self.obs[p] = np.matrix(self.obs[p]).T

        self.start_prob = np.matrix(start_prob).T

        self.input_dim = self.ins[0].shape[1]  # the dimension of input

        # construct the transition weighted matrix
        self.trans_weight_mat = np.random.random(self.n_components * self.n_components * self.input_dim)
        self.trans_weight_mat = self.trans_weight_mat.reshape(self.n_components, self.n_components, self.input_dim)
        #self.trans_weight_mat = np.ones((self.n_components, self.n_components, self.input_dim))
        # construct the weight matrix for poisson regression
        self.obs_weight_mat = np.random.random(self.n_components * self.input_dim)
        self.obs_weight_mat = self.obs_weight_mat.reshape(self.n_components, self.input_dim)


    def fit(self):
        """ Estimate the model parameters

        An initialization step is performed before enter te EM algorithm
        :param obs:
            obs : list
                list of array-like observation sequences, each of which has shape
                (n_i, n_features), which n_i is the length of the i_th observation

        """
        obs = self.obs
        ins = self.ins
        log_prob = []
        for n in range(self.n_iter):
            # Expectation step
            for i in range(len(obs)):
                obs_seq = obs[i]
                ins_seq = ins[i]

                log_trans_mat = self._compute_log_transmat(ins_seq)  # compute dynamic transition matrix with shape (t, n, n)
                log_frame_prob = self._compute_log_obs_prob(ins_seq, obs_seq)  # compute p(y|U, x_t=i) with shape (t, n)

                log_likelihood, fwd_lattice = self._do_forward_pass(log_trans_mat, log_frame_prob)
                bwd_lattice = self._do_backward_pass(log_trans_mat, log_frame_prob)

                # compute the sufficient statistic: transition posterior and state posterior
                self._compute_sufficient_static(log_trans_mat, log_frame_prob,
                                                fwd_lattice, bwd_lattice, log_likelihood)
            log_prob.append(log_likelihood)
            print n,'--------------log_prob---------,', log_prob[n]
            if n > 1 and abs(log_prob[-1] - log_prob[-2]) < self.thresh:
                print 'Converged'
                #break

            # Maximization step
            self._do_maxstep()
        print "Fitted Results:  "
        print "trans_weight_mat", self.trans_weight_mat
        print "obs_weight_mat", self.obs_weight_mat
        print "state_posts Probability:", self.state_posts


    def optimize_trans_beta(self, ins_seq, obs_seq, j, n_iter, threshold=1e-3):
        trans_theta = np.matrix(self.trans_weight_mat[j])
        trans_post = np.matrix(self.trans_posts[:, j, :])
        X = ins_seq
        Y = obs_seq
        difference = []

        for n in range(n_iter):
            jac_array = np.zeros((self.n_components, self.input_dim))
            I = np.matrix(np.identity(self.n_components))
            nu = X * trans_theta.T
            prob_mu = extmath.safe_softmax(nu)
            for s in range(self.n_components):
                I_s = I[:, s]
                prob_mu_s = np.squeeze(np.array(prob_mu[:, s]))
                jac_s = np.squeeze(np.array(X.T * trans_post * I_s)) - \
                        np.squeeze(np.array(X.T *
                                            dia_matrix((prob_mu_s, 0), shape=(len(prob_mu_s), len(prob_mu_s))) *
                                            np.sum(trans_post, axis=1))) - 2 * self.gamma * np.squeeze(trans_theta[s,:])

                jac_array[s, :] = jac_s

                # check for the NAN in records
                if np.isnan(np.min(jac_s)):
                    print 'Encounter NAN', jac_s, n, s, jac_array
                    print 'Debug: '
                    print "trans_post: ", trans_post
                    print "prob_mu_s", prob_mu_s
                    print "I_s", I_s

                    sys.exit()
            jac_vec = np.matrix(jac_array.reshape(self.input_dim * self.n_components, 1))
            hess_array = np.zeros((self.input_dim * self.n_components, self.input_dim * self.n_components))

            for s in range(self.n_components):
                sum_trans_post = np.squeeze(np.array(np.sum(trans_post, axis=1)))
                for p in range(self.n_components):
                    I_sp = I[s, p]
                    prob_s = prob_mu[:, s]
                    prob_p = prob_mu[:, p]
                    prob_item = np.squeeze(np.array(np.multiply(prob_s, prob_p) - I_sp * prob_s))
                    hess_item = X.T * dia_matrix((prob_item, 0), shape=(len(prob_item), len(prob_item))) * dia_matrix((sum_trans_post, 0), shape=(len(sum_trans_post), len(sum_trans_post))) * X - 2 * self.gamma * np.matrix(np.identity(self.input_dim))
                    hess_array[(s * self.input_dim):((s + 1) * self.input_dim), (p * self.input_dim):((p + 1) * self.input_dim)] = np.array(hess_item)

            hess_array = np.matrix(hess_array)
            trans_theta_old = trans_theta

            try:
                trans_theta = trans_theta - np.reshape(np.linalg.pinv(hess_array) * jac_vec, (self.n_components, self.input_dim))
            except Exception as e:
                print 'Failed to Converge!'
                print 'jac_vec', jac_vec
                print hess_array
                sys.exit()
            #print "trans_theta_old ", trans_theta_old
            #print "trans_theta_new", trans_theta
            difference.append(np.max(trans_theta_old - trans_theta))
            if difference[-1] <= threshold:
                break
        self.trans_weight_mat[j, :, :] = np.array(trans_theta)

    def optimize_obs_beta(self, ins_seq, obs_seq, j, n_iter, threshold=1e-3):
        Y = obs_seq
        X = ins_seq

        obs_beta = np.matrix(self.obs_weight_mat[j]).T
        g = np.squeeze(self.state_posts[:, j])
        diag_g = dia_matrix(([g], 0), shape=(len(g), len(g)))
        difference = []

        log_g = np.matrix(self.log_state_posts[:, j]).T

        g_y = np.multiply(np.matrix(self.state_posts[:, j]).T, Y)

        for n in range(n_iter):
            """
            nu = X * obs_beta
            g_nu = np.exp(log_g + nu)
            w_data = np.squeeze(np.array(g_nu))
            W = dia_matrix(([w_data], 0), shape=(len(w_data), len(w_data)))
            grad = X.T * (g_y - g_nu)
            hess = -1 * X.T * diag_g * W * X
            """
            nu = X * obs_beta
            mu = np.exp(nu)
            w_data = np.squeeze(np.array(mu))
            W = dia_matrix(([w_data], 0), shape=(len(w_data), len(w_data)))
            grad = X.T * diag_g * (Y - mu) - 2 * self.gamma * obs_beta
            hess = -1 * X.T * diag_g * W * X - 2 * self.gamma * np.matrix(np.identity(self.input_dim))

            beta_old = obs_beta
            try:
                obs_beta = obs_beta - np.linalg.pinv(hess) * grad
            except Exception as e:
                print 'grad', n, grad, log_g[1:4], nu[1:4]
                sys.exit()
            difference.append(np.max(beta_old - obs_beta))
            if difference[-1] <= threshold:
                break
        self.obs_weight_mat[j, :] = np.squeeze(np.array(obs_beta))


    def _do_maxstep(self):
        # do maximization step in HMM. In base class we do M step to update the parameters for transition
        # weight matrix
        # Based on Yoshua Bengio, Paolo Frasconi. Input output HMM's for sequence processing

        # Maximize the observation subnetwork
        n_iter = 30
        for p in range(len(self.ins)):
            ins_seq = self.ins[p]
            obs_seq = self.obs[p]
            for j in range(self.n_components):
                self.optimize_trans_beta(ins_seq, obs_seq, j, n_iter, threshold=1e-3)
                self.optimize_obs_beta(ins_seq, obs_seq, j, n_iter, threshold=1e-3)

    def _compute_log_transmat(self, ins_seq):
        """ Compute the dynamic transition weight matrix for each time step
        phi_(ij,t) = p(x_t=i|x_{t-1}=j, u_t). In the weight matrix w[j, i] = p(x_t+1=i | x_t=j)
        """
        # initiate the dynamic transition matrix
        log_trans_mat = np.tile(0.0, (len(ins_seq), self.n_components, self.n_components))
        for t in range(len(ins_seq)):
            u = ins_seq[t].T # transform u into column vector
            for j in range(self.n_components):
                weight_mat = np.matrix(self.trans_weight_mat[j])
                alphas = np.squeeze(np.array(weight_mat * u))
                prob = alphas - extmath.logsumexp(alphas)
                log_trans_mat[t, j, :] = prob
        return log_trans_mat

    def _compute_log_obs_prob(self, ins_seq, obs_seq):
        """
        Compute the poisson regression probability
        """
        obs_weight_mat = np.matrix(self.obs_weight_mat)
        log_prob = np.zeros((len(ins_seq), self.n_components))
        for j in range(self.n_components):
            nu = ins_seq * obs_weight_mat[j].T
            lam = np.exp(nu)
            log_prob[:, j] = np.squeeze(np.array(extmath.log_poisson_likelihood(lam, obs_seq)))
        return log_prob

    def _compute_sufficient_static(self, log_trans_mat, log_frame_prob, log_fwd_lattice, log_bwd_lattice, log_likelihood):
        if np.isnan(np.min(log_fwd_lattice)) or np.isnan(np.min(log_bwd_lattice)):
            print log_fwd_lattice, log_bwd_lattice
            sys.exit()

        log_start_prob = np.log(self.start_prob)
        T = len(log_frame_prob)
        # compute the transition posterior
        trans_posts = np.tile(.0, (T, self.n_components, self.n_components))
        # Initiate the first step
        for j in range(self.n_components):
            for i in range(self.n_components):
                trans_posts[0][j][i] = np.exp(log_frame_prob[0, i] + log_start_prob[j, 0] + log_bwd_lattice[0, i] + log_trans_mat[0, j, i] - log_likelihood)

        for t in range(1, T):
            for j in range(self.n_components):
                for i in range(self.n_components):
                    trans_posts[t][j][i] = np.exp(log_frame_prob[t, i] + log_fwd_lattice[t-1, j] + log_bwd_lattice[t, i] + log_trans_mat[t, j, i] - log_likelihood)

        self.trans_posts = trans_posts
        # compute the state posterior
        state_posts = np.zeros((T, self.n_components))
        log_state_posts = np.zeros((T, self.n_components))
        for t in range(T):
            for i in range(self.n_components):
                state_posts[t][i] = np.exp(log_fwd_lattice[t, i] + log_bwd_lattice[t, i] - log_likelihood)
                log_state_posts[t][i] = log_fwd_lattice[t, i] + log_bwd_lattice[t, i] - log_likelihood
        self.state_posts = state_posts
        self.log_state_posts = log_state_posts
        print '--------trans_posts--------', trans_posts

    def _do_forward_pass(self, log_trans_mat, log_frame_prob):
        """  Compute the forward lattice
        :param log_trans_mat:
        :param log_frame_prob:
        :return: p(obs_seq|ins_seq) and p(x_t=i, y_(1:t)|u_(1:t))
        """
        print '-----log_trans_mat---', np.exp(log_trans_mat)
        T = len(log_frame_prob)
        log_fwd_lattice = np.zeros((T, self.n_components))
        log_start_prob = np.log(self.start_prob)
        for i in range(self.n_components):
            work_buffer = np.zeros(self.n_components)
            for j in range(self.n_components):
                work_buffer[j] = log_start_prob[j, 0] + log_trans_mat[0][j][i]

            log_fwd_lattice[0][i] = log_frame_prob[0][i] + extmath.logsumexp(work_buffer)

        for t in range(1, T):
            for i in range(self.n_components):
                for j in range(self.n_components):
                    work_buffer[j] = log_fwd_lattice[t - 1][j] + log_trans_mat[t][j][i]
                log_fwd_lattice[t][i] = log_frame_prob[t][i] + extmath.logsumexp(work_buffer)
        log_likelihood = extmath.logsumexp(log_fwd_lattice[-1, :])
        return log_likelihood, log_fwd_lattice

    def _do_backward_pass(self, log_trans_mat, log_frame_prob):
        # using the same scaling_factor as forward_pass
        T = len(log_frame_prob)
        log_bwd_lattice = np.ones((T, self.n_components))
        work_buffer = np.zeros(self.n_components)
        for i in range(self.n_components):
            log_bwd_lattice[T - 1][i] = 0.0

        for t in range(T - 2, -1, -1):
            for i in range(self.n_components):
                for j in range(self.n_components):
                    work_buffer[j] = log_bwd_lattice[t + 1, j] + log_frame_prob[t + 1][j] + log_trans_mat[t + 1][i][j]
                log_bwd_lattice[t][i] = extmath.logsumexp(work_buffer)
        return log_bwd_lattice
