import sys
import numpy as np
import threading
import matplotlib.pyplot as plt
from os.path import exists
import os
import pickle

sys.path.insert(1, 'C:/Users/ASUser/Downloads/TorchBO/')
from Nion_interface import Nion_interface
from TorchCNN import Net

import torch
from botorch.models import SingleTaskGP
from botorch.fit import fit_gpytorch_model
from gpytorch.mlls import ExactMarginalLogLikelihood
from botorch.optim import optimize_acqf
from botorch.acquisition import UpperConfidenceBound
from botorch.models.transforms.outcome import Standardize

# TODO: Read the default parameter before running optimization.
# TODO: Add option to set to default or set to best seen parameter.
class BOinterface():
    '''
    Main function that set up the input parameters and run the Bayesian optimization.
    '''
    def __init__(self, abr_activate, option_standardize, aperture, CNNpath, filename, exposure_t, remove_buffer, scale_option, acq_func_par):

        # setup basic parameters
        self.ndim = sum(abr_activate)            # number of variable parameters
        self.abr_activate = abr_activate
        self.aperture = aperture            # option to include cutoff aperture, in mrad
        self.n_measurement = 0              # counter for number of measurements
        self.dtype = torch.double
        self.option_standardize = option_standardize
        self.CNNoption = 1
        self.abr_activate = self.abr_activate
        self.scale_option = scale_option        # option to determine whether normalize with aperture considered, 0 for not considering aperture
        # self.device = ("cuda" if torch.cuda.is_available() else "cpu")
        # self.device = torch.device('cuda:1')   # possible command to select from multiple GPUs
        self.device = "cpu"                 # hard coded to cpu for now, need to find a way to move all the model weights to the desired device
        self.acq_func_par = acq_func_par
        self.CNNpath = CNNpath 

        # initialize the interface that talks to Nion swift.
        self.Nion = Nion_interface(act_list = abr_activate, readDefault = True, detectCenter = True, exposure_t = exposure_t, remove_buffer = remove_buffer)
        self.default = self.Nion.default
        
        # initialize the CNN model used to run predictions.
        self.model = self.loadCNNmodel(self.CNNpath)
        self.model.eval()

        # initialize the lists to save the results
        self.best_observed_value = []
        self.best_seen_ronchigram = np.zeros([128, 128])
        self.best_par = np.zeros(self.ndim)
        self.ronchigram_list = []

        # readin the name for saving the results
        self.filename = filename

    
    def loadCNNmodel(self, path):
        '''
        Function to load CNN model from path.
        Input: path to the torch model.
        '''
        state_dict = torch.load(path, map_location = self.device)
        model = Net(device = self.device, linear_shape = state_dict['fc1.weight'].shape[0]).to(self.device)
        model.load_state_dict(state_dict)
        return model

    def getCNNprediction(self):
        '''
        Function to set objective based on CNN prediction. Return the raw frame without any rescale.
        Input: 128x128 numpy array as the input to CNN.
        '''
        acquire_thread = threading.Thread(target = self.Nion.acquire_frame())
        acquire_thread.start()
        frame_array = self.Nion.frame
        frame_array_raw = self.Nion.frame
        if self.scale_option:
            frame_array = self.Nion.scale_range_aperture(frame_array, 0, 1, self.aperture[0], self.scale_option)
        else:
            frame_array = self.Nion.scale_range(frame_array, 0, 1)
        if self.aperture[1] != 0:
            frame_array = frame_array * self.Nion.aperture_generator(128, self.aperture[0], self.aperture[1])
        new_channel = np.zeros(frame_array.shape)
        img_stack = np.dstack((frame_array, new_channel, new_channel))
        x = torch.tensor(np.transpose(img_stack)).to(self.device)
        x = x.unsqueeze(0).float()
        prediction = self.model(x)
        return frame_array_raw, 1 - prediction[0][0].cpu().detach().numpy()

    def initialize_GP(self, n):
        '''
        Function that initialize the GP and MLL model, with n random starting points.
        Input:
        n: int, number of datapoints to gerate
        '''
        # generate random initial training data
        self.train_X = torch.rand(n, self.ndim, device = self.device, dtype = self.dtype)
        output_y = []
        best_y = 0

        for i in range(self.train_X.shape[0]):
            self.Nion.setX(np.array(self.train_X[i,:]))
            pred = self.getCNNprediction()
            if pred[1] > best_y:
                self.best_seen_ronchigram = pred[0]
                best_y = pred[1]
            output_y.append(pred[1])
        self.train_Y = torch.tensor(output_y).unsqueeze(-1)

        if self.option_standardize:
            self.outcome_transformer = Standardize( m = 1,
            batch_shape = torch.Size([]),
            min_stdv = 1e-08)
            self.gp = SingleTaskGP(self.train_X, self.train_Y, outcome_transform = self.outcome_transformer)
        else:
            self.gp = SingleTaskGP(self.train_X, self.train_Y)
            
        self.mll = ExactMarginalLogLikelihood(self.gp.likelihood, self.gp)
        self.bounds = torch.stack([torch.zeros(self.ndim, device = self.device), torch.ones(self.ndim, device = self.device)])
        self.best_observed_value.append(best_y)
        self.ronchigram_list.append(self.best_seen_ronchigram)
        self.n_measurement += n
        return

    def run_iteration(self):
        '''
        Function to run one iteration on the Bayesian optimization and update both gp and mll.
        '''
        fit_gpytorch_model(self.mll)
        UCB = UpperConfidenceBound(self.gp, beta = self.acq_func_par[1])  # TODO: add more options of acquisition functions.
        # TODO: Check the option here in the optimize_acqf  
        candidate, acq_value = optimize_acqf(
            UCB, bounds=self.bounds, q = 1, num_restarts=5, raw_samples=20,
        )
        new_x = candidate.detach()
        print(new_x.cpu().detach().numpy()) # TODO: convert the output to physical units.
        
        self.Nion.setX(np.array(new_x[0]))
        result = self.getCNNprediction()
        new_y = torch.tensor(result[1]).unsqueeze(-1).unsqueeze(-1)
        self.train_X = torch.cat([self.train_X, new_x])
        self.train_Y = torch.cat([self.train_Y, new_y])

        if result[1] > self.best_observed_value[-1]:
            self.best_par = np.array(new_x[0])
            self.best_value = result[1]
            self.best_seen_ronchigram = result[0]
            self.best_observed_value.append(result[1])
        else:
            self.best_observed_value.append(self.best_observed_value[-1])
        self.ronchigram_list.append(result[0])

        # update GP model using dataset with new datapoint
        if self.option_standardize:
            self.gp = SingleTaskGP(self.train_X, self.train_Y, outcome_transform = self.outcome_transformer)
        else:
            self.gp = SingleTaskGP(self.train_X, self.train_Y)
        self.mll = ExactMarginalLogLikelihood(self.gp.likelihood, self.gp)

    def run_optimization(self, niter):
        '''
        Function to run the full Bayesian optimization for niter iterations.
        Input:
        niter: int, number of iterations to run
        '''
        for i in range(niter):
            self.run_iteration()
            print(f"Iteraton number {i}, current value {self.train_Y[-1].cpu().detach().numpy()}, current best seen value {self.best_observed_value[-1]}")
        self.n_measurement += niter
        return

    def DataGenerator(self) -> dict:
        '''
        function that combines all the collected data and metadata into a single package for saving purpose.
        '''
        package = {}
        # save the metadata of BO
        package['abr_activate'] = self.abr_activate
        package['option_standardize'] = self.option_standardize
        package['aperture'] = self.aperture
        package['CNNpath'] = self.CNNpath
        package['acq_func'] = self.acq_func_par
        package['scale_option'] = self.scale_option
        package['total_measurements'] = self.n_measurement
        package['abr_limit'] = self.Nion.abr_lim

        # save the observations of BO
        train_X = self.train_X.cpu().detach().numpy()
        train_Y = self.train_Y.cpu().detach().numpy()
        package['X'] = train_X
        package['Y'] = train_Y
        package['Ronchigram'] = np.array(self.ronchigram_list)
        return package

    def saveresults(self):
        '''
        Function ot save the parameters and results of Bayesian optimization.
        '''
        if not exists(self.filename):
            os.mkdir(self.filename)
        index = 0
        temp = self.filename + '/Results_' + "{:02d}".format(index) + '.npy'
        while exists(temp):
            index += 1
            temp = self.filename + '/Results_' + "{:02d}".format(index) + '.npy'
        data_package = self.DataGenerator()
        with open(self.filename + '/Results_' + "{:02d}".format(index) + '.pkl', 'wb') as f:
            pickle.dump(data_package, f)
        return


    def plotresults(self):
        '''
        Function to plot the Bayesian optimization results.
        TODO: Possibly add the model's prediction of a single dimension.
        Input: 
        best_observed_value: numpy array saving the best observed value for each iteration.
        best_seen_ronchigran: 2D numpy array saving the ronchigram that correspond to the optimized parameter.

        '''
        niter = len(self.train_Y)
        ninit = len(self.train_Y) - len(self.best_observed_value)
        fig, ax = plt.subplots(3,1,figsize = [7,18])
        ax[0].plot(np.linspace(ninit + 1, niter, len(self.best_observed_value)), self.best_observed_value, label = 'Best seen value')
        ax[0].plot(np.linspace(1, niter, niter), self.train_Y, label = 'Observations')
        ax[0].set_xlabel('Iterations',fontsize = 16)
        ax[0].set_ylabel('CNN prediction', fontsize = 16)
        ax[0].tick_params(axis='both', labelsize=16)
        ax[0].axvline(x = ninit + 1, ls = '--', color = 'black')
        ax[0].legend(fontsize = 16)

        index = [i for i, x in enumerate(self.abr_activate) if x]
        for i in range(self.train_X.shape[1]):
            ax[1].plot((self.train_X[:,i] - 0.5) * self.Nion.abr_lim[index[i]], linewidth = 2, label = self.Nion.abr_list[index[i]])

        ax[2].imshow(self.best_seen_ronchigram, cmap = 'gray')
        ax[2].axis('off')
        ax[2].legend(fontsize = 16)
        plt.show()
        return