# /usr/bin/env python
import inspect
import time

import dask
import dask.distributed as distributed
import numpy as np
from loguru import logger

from gpcam.data import fvgpData, gpData
from gpcam.gp_optimizer import GPOptimizer, fvGPOptimizer


class AutonomousExperimenterGP():
    """
    Executes the autonomous loop for a single-task Gaussian process.
    Use class AutonomousExperimenterfvGP for multi-task experiments.

    Parameters
    ----------
    input_space_bounds : np.ndarray
        A numpy array of floats of shape D x 2 describing the input space.
    hyperparameters : np.ndarray
        A 1-D numpy array of floats. The default kernel function expects a length of D+1, where the first
        value is a signal variance, followed by a length scale in each direction of the input space. If a kernel
        function is provided, then the expected length is determined by that function.
    hyperparameter_bounds : np.ndarray
        A 2-D array of floats of size J x 2, such that J is the length matching the length of `hyperparameters` defining
        the bounds for training.
    instrument_func : Callable, optional
         A function that takes data points (a list of dicts), and returns a similar structure with data filled in. The function is
         expected to communicate with the instrument and perform measurements, populating fields of the data input.
    init_dataset_size : int, optional
        If `x` and `y` are not provided and `dataset` is not provided, `init_dataset_size` must be provided. An initial
        dataset is constructed randomly with this length. The `instrument_func` is immediately called to measure values
        at these initial points.
    acq_func : Callable, optional
        The acquisition function accepts as input a numpy array of size V x D (such that V is the number of input
        points, and D is the parameter space dimensionality) and a `GPOptimizer` object. The return value is 1-D array
        of length V providing 'scores' for each position, such that the highest scored point will be measured next.
        Built-in functions can be used by one of the following keys: `'shannon_ig'`, `'shannon_ig_vec'`, `'ucb'`, `'maximum'`, `'minimum'`,
        `'covariance'`, `'variance'`, and `'gradient'`. If None, the default function is the `'variance'`, meaning
        `fvgp.gp.GP.posterior_covariance` with variance_only = True.
    cost_func : Callable, optional
        A function encoding the cost of motion through the input space and the cost of a measurement. Its inputs are an
        `origin` (np.ndarray of size V x D), `x` (np.ndarray of size V x D), and the value of `cost_func_params`;
        `origin` is the starting position, and `x` is the destination position. The return value is a 1-D array of
        length V describing the costs as floats. The 'score' from acq_func is divided by this returned cost to determine
        the next measurement point. If None, the default is a uniform cost of 1.
    cost_update_func : Callable, optional
        A function that updates the `cost_func_params` which are communicated to the `cost_func`. This accepts as input
        costs (a list of cost values determined by `instrument_func`), bounds (a V x 2 numpy array) and parameters
        object. The default is a no-op.
    cost_func_params : Any, optional
        An object that is communicated to the `cost_func` and `cost_update_func`. The default is `{}`.
    kernel_func : Callable, optional
        A function that calculates the covariance between data points. It accepts as input x1 (a V x D array of positions),
        x2 (a U x D array of positions), hyperparameters (a 1-D array of length D+1 for the default kernel), and a
        `gpcam.gp_optimizer.GPOptimizer` instance. The default is a stationary anisotropic kernel
        (`fvgp.gp.GP.default_kernel`).
    prior_mean_func : Callable, optional
        A function that evaluates the prior mean at an input position. It accepts as input a
        `gpcam.gp_optimizer.GPOptimizer` instance, an array of positions (of size V x D), and hyperparameters (a 1-D
        array of length D+1 for the default kernel). The return value is a 1-D array of length V. If None is provided,
        `fvgp.gp.GP.default_mean_function` is used.
    run_every_iteration : Callable, optional
        A function that is run at every iteration. It accepts as input this
        `gpcam.autonomous_experimenter.AutonomousExperimenterGP` instance. The default is a no-op.
    x_data : np.ndarray, optional
        Initial data point positions
    y_data : np.ndarray, optional
        Initial data point values
    variances : np.ndarray, optional
        Initial data point observation variances
    dataset : string, optional
        A filename of a gpcam-generated file that is used to initialize a new instance.
    communicate_full_dataset : bool, optional
        If True, the full dataset will be communicated to the `instrument_func` on each iteration. If False, only the
        newly suggested data points will be communicated. The default is False.
    compute_device : str, optional
        One of "cpu" or "gpu", determines how linear system solves are run. The default is "cpu".
    store_inv : bool, optional
        If True, the algorithm calculates and stores the inverse of the covariance matrix after each training or update of the dataset,
        which makes computing the posterior covariance faster.
        For larger problems (>2000 data points), the use of inversion should be avoided due to computational instability. The default is
        False. Note, the training will always use a linear solve instead of the inverse for stability reasons.
    training_dask_client : distributed.client.Client, optional
        A Dask Distributed Client instance for distributed training. If None is provided, a new
        `dask.distributed.Client` instance is constructed.
    acq_func_opt_dask_client : distributed.client.Client, optional
        A Dask Distributed Client instance for distributed `acquisition_func` computation. If None is provided, a new
        `dask.distributed.Client` instance is constructed.
    info : bool, optional
        bool specifying if the should be extensive std out. Default = False


    Attributes
    ----------
    x_data : np.ndarray
        Data point positions
    y_data : np.ndarray
        Data point values
    variances : np.ndarray
        Data point observation variances
    data.dataset : list
        All data
    hyperparameter_bounds : np.ndarray
        A 2-D array of floats of size J x 2, such that J is the length matching the length of `hyperparameters` defining
        the bounds for training.
    gp_optimizer : gpcam.gp_optimizer.GPOptimizer
        A GPOptimizer instance used for initializing a gaussian process and performing optimization of the posterior.


    """

    def __init__(self,
                 input_space_bounds,
                 hyperparameters,
                 hyperparameter_bounds,
                 instrument_func=None,
                 init_dataset_size=None,
                 acq_func="variance",
                 cost_function=None,
                 cost_update_function=None,
                 cost_function_parameters={},
                 kernel_func=None,
                 prior_mean_func=None,
                 noise_function=None,
                 run_every_iteration=None,
                 x_data=None, y_data=None, noise_variances=None, dataset=None,
                 communicate_full_dataset=False,
                 compute_device="cpu",
                 store_inv=False,
                 training_dask_client=None,
                 acq_func_opt_dask_client=None,
                 sparse_mode = False,
                 gp2Scale = False,
                 gp2Scale_dask_client = None,
                 gp2Scale_batch_size = 10000,
                 normalize_y = False,
                 ram_economy=True,
                 info=False,
                 args = None
                 ):
        if info:
            logger.enable('gpcam')
            logger.enable('fvgp')
            logger.enable('hgdl')
        else:
            logger.disable('gpcam')
            logger.disable('fvgp')
            logger.disable('hgdl')


        dim = len(input_space_bounds)
        self.input_space_bounds = input_space_bounds
        self.instrument_func = instrument_func
        self.hyperparameter_bounds = hyperparameter_bounds
        self.hyperparameters = hyperparameters
        self.acq_func = acq_func
        self.run_every_iteration = run_every_iteration
        self.communicate_full_dataset = communicate_full_dataset
        self.async_train_in_progress = False
        self.training_dask_client = training_dask_client
        self.acq_func_opt_dask_client = acq_func_opt_dask_client
        self.args = args
        ################################
        # getting the data ready#########
        ################################
        if init_dataset_size is None and x_data is None and dataset is None:
            raise Exception("Either provide length of initial data or an inital dataset")
        self.data = gpData(dim, input_space_bounds)
        if x_data is None and dataset is None:
            self.data.create_random_dataset(init_dataset_size)
            if instrument_func is None: raise Exception("You need to provide an instrument function.")
            self.data.dataset = self.instrument_func(self.data.dataset)
        elif dataset is not None:
            self.data.inject_dataset(list(np.load(dataset, allow_pickle=True)))
            hyperparameters = self.data.dataset[-1]["hyperparameters"]
        elif x_data is not None and y_data is not None:
            self.data.dataset = self.data.inject_arrays(x_data, y=y_data, v=noise_variances)
        elif x_data is not None and y_data is None:
            if instrument_func is None: raise Exception("You need to provide an instrument function.")
            self.data.dataset = self.instrument_func(self.data.inject_arrays(x_data, y=y_data, v=noise_variances))
        else:
            raise Exception("No viable option for data given!")
        self.data.check_incoming_data()
        if self.data.nan_in_dataset(): self.data.clean_data_NaN()
        self.x_data, self.y_data, self.noise_variances, self.times, self.cost = self.data.extract_data()
        self.init_dataset_size = len(self.x_data)
        ######################
        ######################
        ######################
        self.gp_optimizer = GPOptimizer(self.x_data, self.y_data,
            init_hyperparameters = hyperparameters,
            noise_variances = self.noise_variances,
            compute_device = compute_device,
            gp_kernel_function = kernel_func,
            gp_kernel_function_grad = None,
            gp_noise_function = noise_function,
            gp_noise_function_grad = None,
            gp_mean_function = prior_mean_func,
            gp_mean_function_grad = None,
            sparse_mode = sparse_mode,
            gp2Scale = gp2Scale,
            gp2Scale_dask_client = gp2Scale_dask_client,
            gp2Scale_batch_size = gp2Scale_batch_size,
            normalize_y = normalize_y,
            store_inv = store_inv,
            ram_economy = ram_economy,
            args = args,
            info = info,
            cost_function = cost_function,
            cost_function_parameters = cost_function_parameters,
            cost_update_function = cost_update_function)



        # init costs
        logger.info(inspect.cleandoc("""#
        ##################################################################################
        Autonomous Experimenter initialization successfully concluded
        now train(...) or train_async(...), and then go(...)
        ##################################################################################"""))

    ###################################################################################
    def train(self, init_hyperparameters = None, pop_size=10, tol=1e-6, max_iter=20, method="global", constraints = ()):
        """
        Function to train the Gaussian Process. The use is entirely optional; this function will be called
        as part of the go() command.

        Parameters
        ----------
        pop_size : int, optional
            The number of individuals in case method=`'global'`. Default = 10
        tol : float, optional
            Convergence tolerance for the local optimizer (if method = `'local'`)
            Default = 1e-6
        max_iter : int, optional
            Maximum number of iterations for the global method. Default = 20
        method : str, optional
            Method to be used for the training. Default is `'global'` which means
            a differetnial evolution algorithm is run with the specified parameters.
            The options are `'global'` or `'local'`, or `'mcmc'`.
        """

        self.gp_optimizer.train_gp(
            hyperparameter_bounds = self.hyperparameter_bounds,
            init_hyperparameters = init_hyperparameters,
            method=method,
            pop_size=pop_size,
            tolerance=tol,
            max_iter=max_iter,
            constraints = constraints)

    def train_async(self, init_hyperparameters = None, max_iter=10000, dask_client=None, local_method="L-BFGS-B", global_method="genetic", constraints = ()):
        """
        Function to train the Gaussian Process asynchronously using the HGDL optimizer. 
        The use is entirely optional; this function will be called
        as part of the go() command, if so specified. This call starts a highly parallelized optimization process,
        on an architecture specified by the dask.distributed.Client. The main purpose of this function is to
        allow for large-scale distributed training. 

        Parameters
        ----------
        max_iter : int, optional
            Maximum number of iterations for the global method. Default = 10000
            It is important to remember here that the call is run asynchronously, so
            this number does not affect run time.
        local_method : str, optional
            Local method to be used inside HGDL. Many scipy.optimize.minimize methods
            can be used, or a user-defined callable. Please read the HGDL docs for more information.
            Default = `'L-BFGS-B'`.
        global_method : str, optional
            Local method to be used inside HGDL. Please read the HGDL docs for more information.
            Default = `'genetic'`.
        """

        if dask_client: self.training_dask_client = dask_client
        else: self.training_dask_client = dask.distributed.Client()

        logger.info("AutonomousExperimenter starts async training with dask client:")
        self.opt_obj = self.gp_optimizer.train_gp_async(
            hyperparameter_bounds = self.hyperparameter_bounds,
            init_hyperparameters = init_hyperparameters,
            max_iter=max_iter,
            local_optimizer=local_method,
            global_optimizer=global_method,
            constraints = constraints,
            dask_client=self.training_dask_client
            )
        logger.info("The Autonomous Experimenter started an instance of the asynchronous training.")
        self.async_train_in_progress = True

    def kill_training(self):
        """
        Function to kill an active asynchronous training
        """
        if self.async_train_in_progress:
            self.gp_optimizer.stop_async_train(self.opt_obj)
            self.async_train_in_progress = False
        else:
            logger.info("no training to be killed")

    def kill_all_clients(self):
        """
        Function to kill both dask.distibuted.Client instances.
        Will be called automatically at the end of go().
        """
        try:
            self.training_dask_client.close()
            self.acq_func_opt_dask_client.close()
        except Exception as ex:
            logger.error(str(ex))
            logger.error("Killing of the clients failed. Please do so manually before initializing a new one.")

    def update_hps(self):
        """
        Function to update the hyperparameters if an asynchronous training is running.
        Will be called during go() as specified.
        """
        if self.async_train_in_progress:
            self.gp_optimizer.update_hyperparameters(self.opt_obj)
            logger.info("The Autonomus Experimenter updated the hyperparameters")
        else:
            logger.warning("The autonomous experimenter could not find an instance of asynchronous training. Therefore no update.")
        logger.info("hps: {}", self.gp_optimizer.hyperparameters)

    def _tell(self, x, y, v, vp=None):
        if vp is None:
            self.gp_optimizer.tell(x, y, noise_variances=v)
        else:
            self.gp_optimizer.tell(x, y, noise_variances=v, output_positions=vp)

    def _extract_data(self):
        x, y, v, t, c = self.data.extract_data()
        return x, y, v, t, c, None

    ###################################################################################
    def go(self, N=1e15, breaking_error=1e-50,
           retrain_globally_at=(20, 50, 100, 400, 1000),
           retrain_locally_at=(20, 40, 60, 80, 100, 200, 400, 1000),
           retrain_async_at=(),
           update_cost_func_at=(),
           acq_func_opt_setting=lambda number: "global",
           training_opt_max_iter=20,
           training_opt_pop_size=10,
           training_opt_tol=1e-6,
           acq_func_opt_max_iter=20,
           acq_func_opt_pop_size=20,
           acq_func_opt_tol=1e-6,
           acq_func_opt_tol_adjust=0.1,
           number_of_suggested_measurements=1,
           checkpoint_filename=None,
           constraints = (),
           ask_args = None,
           break_condition_callable = lambda n: False
           ):
        """
        Function to start the autonomous-data-acquisition loop.

        Parameters
        ----------
        N : int, optional
            Run until N points are measured. The default is `1e15`.
        breaking_error : float, optional
            Run until breaking_error is achieved (or at max N). The default is `1e-50`.
        retrain_globally_at : Iterable[int], optional
            Retrains the hyperparameters at the given number of measurements using global optimization. The deafult is
            `[20,50,100,400,1000]`.
        retrain_locally_at : Iterable[int], optional
            Retrains the hyperparameters at the given number of measurements using local gradient-based optimization.
            The default is `[20,40,60,80,100,200,400,1000]`.
        retrain_async_at : Iterable[int], optional
            Retrains the hyperparameters at the given number of measurements using the HGDL algorithm. This training is
            asynchronous and can be run in a distributed fashion using `training_dask_client`. The default is `[]`.
        update_cost_func_at : Iterable[int], optional
            Calls the `update_cost_func` at the given number of measurements.
            Default = ()
        acq_func_opt_setting : Callable, optional
            A callable that accepts as input the iteration index and returns either `'local'`, `'global'`, `'hgdl'`. This
            switches between local gradient-based, global and hybrid optimization for the acquisition function.
            The default is `lambda number: "global" if number % 2 == 0 else "local"`.
        training_opt_max_iter : int, optional
            The maximum number of iterations for any training.
            The default value is 20.
        training_opt_pop_size : int, optional
            The population size used for any training with a global component (HGDL or standard global optimizers).
            The default value is 10.
        training_opt_tol : float, optional
            The optimization tolerance for all training optimization. The default is 1e-6.
        acq_func_opt_max_iter : int, optional
            The maximum number of iterations for the `acq_func` optimization. The default is 20.
        acq_func_opt_pop_size : int, optional
            The population size used for any `acq_func` optimization with a global component (HGDL or standard global
            optimizers). The default value is 20.
        acq_func_opt_tol : float, optional
            The optimization tolerance for all `acq_func` optimization.
            The default value is 1e-6
        acq_func_opt_tol_adjust : float, optional
            The `acq_func` optimization tolerance is adjusted at every iteration as a fraction of this value.
            The default value is 0.1 .
        number_of_suggested_measurements : int, optional
            The algorithm will try to return this many suggestions for new measurements. This may be limited by how many
            optima the algorithm may find. If greater than 1, then the `acq_func` optimization method is automatically
            set to use HGDL. The default is 1.
        checkpoint_filename : str, optional
            When provided, a checkpoint of all the accumulated data will be written to this file on each iteration.
        constraints : tuple, optional
            If provided, this subjects the acquisition function optimization to constraints. For the definition of the constraints, follow
            the structure your chosen optimizer requires.
        break_condition_callable : Callable, optional
            Autonomous loop will stop when this function returns True. The function takes as input a gpcam.autonomous_experimenter instance
        ask_args : dict, optional
            For now, only required for acquisiton function "target probability". In this case it should be
            defined as {"a": some lower bound, "b":some upper bound}, example: "ask_args = {"a": 1.0,"b": 3.0}".

        """
        #set up
        start_time = time.time()
        start_date_time = time.strftime("%Y-%m-%d_%H_%M_%S", time.localtime())
        logger.info("Starting the autonomous loop...")
        i = 0
        n_measurements = len(self.x_data)
        #start the loop
        while n_measurements < N:
            logger.info("----------------------------")
            logger.info(f"iteration {i}")
            logger.info(f"Run Time: {time.time() - start_time} seconds")
            logger.info(f"Number of measurements {n_measurements}")


            # ask() for new suggestions
            current_position = self.x_data[-1]
            logger.info("current hps: {}", self.gp_optimizer.hyperparameters)
            local_method = acq_func_opt_setting(i)
            if number_of_suggested_measurements > 1 and local_method != "hgdl": local_method = "global"

            if local_method == "hgdl" and self.acq_func_opt_dask_client is None: self.acq_func_opt_dask_client = dask.distributed.Client()
            res = self.gp_optimizer.ask(
                self.input_space_bounds,
                position=current_position,
                n=number_of_suggested_measurements,
                acquisition_function=self.acq_func,
                method=local_method,
                pop_size=acq_func_opt_pop_size,
                max_iter=acq_func_opt_max_iter,
                tol=acq_func_opt_tol,
                args = ask_args,
                constraints = constraints,
                dask_client=self.acq_func_opt_dask_client)

            self.acq_func_max_opt_obj = res["opt_obj"]
            next_measurement_points = res["x"]
            func_evals = res["f(x)"]
            if self.data.output_dim:
                a = np.array(next_measurement_points)
                b = np.array(self.vp[-1])
                test_points = np.array([np.append(a[i],b[j]) for i in range(len(a)) for j in range(len(b))]) 
                post_var = self.gp_optimizer.posterior_covariance(test_points)["v(x)"]
            else: post_var = self.gp_optimizer.posterior_covariance(next_measurement_points)["v(x)"]
            error = np.max(np.sqrt(post_var))


            #adjust tolerances if necessary
            if acq_func_opt_tol_adjust:
                acq_func_opt_tol = abs(func_evals[0]) * acq_func_opt_tol_adjust
                logger.info("acquisition function optimization tolerance changed to: {}", acq_func_opt_tol)
            logger.info("Next points to be requested: ")
            logger.info(next_measurement_points)
            # update and tell() new data
            info = [{"hyperparameters": self.gp_optimizer.hyperparameters,
                     "posterior std": np.sqrt(post_var[j])} for j in range(len(next_measurement_points))]
            new_data = self.data.inject_arrays(next_measurement_points, info=info)
            logger.info("Sending request to instrument ...")
            if self.communicate_full_dataset:
                self.data.dataset = self.instrument_func(self.data.dataset + new_data)
            else:
                self.data.dataset = self.data.dataset + self.instrument_func(new_data)


            #receive new data
            logger.info("Data received")
            logger.info("Checking if data is clean ...")
            self.data.check_incoming_data()
            if self.data.nan_in_dataset(): self.data.clean_data_NaN()
            # update arrays and the gp_optimizer
            self.x_data, self.y_data, self.variances, self.times, self.costs, vp = self._extract_data()
            logger.info("Communicating new data to the GP")
            self._tell(self.x_data, self.y_data, self.variances, vp)


            # retrain()
            logger.info(inspect.cleandoc("""#
                ++++++++++++++++++++++++++
                |Training ...            |
                ++++++++++++++++++++++++++"""))
            if any(n in retrain_async_at for n in range(n_measurements,len(self.x_data))) and n_measurements<N:
                if self.training_dask_client is None: self.training_dask_client = dask.distributed.Client()
                logger.info("    Starting a new asynchronous training after killing the current one.")
                self.kill_training()
                self.train_async(max_iter=training_opt_max_iter,
                                 dask_client=self.training_dask_client)
            elif any(n in retrain_globally_at for n in range(n_measurements,len(self.x_data))) and n_measurements<N:
                self.kill_training()
                logger.info("    Fresh optimization from scratch via global optimization")
                self.train(pop_size=training_opt_pop_size,
                           tol=training_opt_tol,
                           max_iter=training_opt_max_iter,
                           method="global")
            elif any(n in retrain_locally_at for n in range(n_measurements,len(self.x_data))) and n_measurements<N:
                self.kill_training()
                logger.info("    Fresh optimization from scratch via local optimization")
                self.train(pop_size=training_opt_pop_size,
                           tol=training_opt_tol,
                           max_iter=training_opt_max_iter,
                           method="local")
            else:
                logger.info("    No training in this round but I am trying to update the hyperparameters")
                self.update_hps()
            logger.info("    Training successfully concluded")

            #run a uder-defined callable
            if self.run_every_iteration is not None: self.run_every_iteration(self)


            #save some data
            if checkpoint_filename:
                try:
                    np.save(checkpoint_filename, self.data.dataset)
                except Exception as e:
                    raise RuntimeError("Data not saved") from e

            # cost update
            if i in update_cost_func_at: self.gp_optimizer.update_cost_function(self.costs)


            #break check
            if error < breaking_error: break
            if break_condition_callable(self): break

            #update iteration numbers
            i += 1
            n_measurements = len(self.x_data)

        #clean up
        logger.info("killing the client... and then we are done")
        self.kill_all_clients()

        logger.info(inspect.cleandoc("""#
            ====================================================
            The autonomous experiment was concluded successfully
            ===================================================="""))


###################################################################################
###################################################################################
###################################################################################
###################################################################################
###################################################################################
###################################################################################
class AutonomousExperimenterFvGP(AutonomousExperimenterGP):
    """
    Executes the autonomous loop for a multi-task Gaussian process.
    Use class AutonomousExperimenterfvGP for multi-task experiments.

    Parameters
    ----------
    input_space_bounds : np.ndarray
        A numpy array of floats of shape D x 2 describing the input space range
    output_number : int
        An integer defining how many outputs are created by each measurement.
    output_dim : int
        The dimensionality of the output space. Most often 1.
    hyperparameters : np.ndarray
        A 1-D numpy array of floats. The default kernel function expects a length of D+1, where the first
        value is a signal variance, followed by a length scale in each direction of the input space. If a kernel
        function is provided, then the expected length is determined by that function.
    hyperparameter_bounds : np.ndarray
        A 2-D array of floats of size J x 2, such that J is the length matching the length of `hyperparameters` defining
        the bounds for training.
    instrument_func : Callable, optional
         A function that takes data points (a list of dicts), and returns a similar structure. The function is
         expected to
         communicate with the instrument and perform measurements, populating fields of the data input. If
    init_dataset_size : int, optional
        If `x` and `y` are not provided and `dataset` is not provided, `init_dataset_size` must be provided. An initial
        dataset is constructed randomly with this length. The `instrument_func` is immediately called to measure values
        at these initial points.
    acq_func : Callable, optional
        The acquisition function accepts as input a numpy array of size V x D (such that V is the number of input
        points, and D is the parameter space dimensionality) and a `GPOptimizer` object. The return value is 1-D array
        of length V providing 'scores' for each position, such that the highest scored point will be measured next.
        Built-in functions can be used by one of the following keys: `'shannon_ig'`, `'shannon_ig_vec'`, `'UCB'`, `'maximum'`, `'minimum'`,
        `'covariance'`, and `'variance'`. If None, the default function is the `'variance'`, meaning
        `fvgp.gp.GP.posterior_covariance` with variance_only = True.
    cost_func : Callable, optional
        A function encoding the cost of motion through the input space and the cost of a measurement. Its inputs are an
        `origin` (np.ndarray of size V x D), `x` (np.ndarray of size V x D), and the value of `cost_func_params`;
        `origin` is the starting position, and `x` is the destination position. The return value is a 1-D array of
        length V describing the costs as floats. The 'score' from acq_func is divided by this returned cost to determine
        the next measurement point. If None, the default is a uniform cost of 1.
    cost_update_func : Callable, optional
        A function that updates the `cost_func_params` which are communicated to the `cost_func`. This accepts as input
        costs (a list of cost values determined by `instrument_func`), bounds (a V x 2 numpy array) and parameters
        object. The default is a no-op.
    cost_func_params : Any, optional
        An object that is communicated to the `cost_func` and `cost_update_func`. The default is `{}`.
    kernel_func : Callable, optional
        A function that calculates the covariance between data points. It accepts as input x1 (a V x D array of positions),
        x2 (a U x D array of positions), hyperparameters (a 1-D array of length D+1 for the default kernel), and a
        `gpcam.gp_optimizer.GPOptimizer` instance. The default is a stationary anisotropic kernel
        (`fvgp.gp.GP.default_kernel`).
    prior_mean_func : Callable, optional
        A function that evaluates the prior mean at an input position. It accepts as input a
        `gpcam.gp_optimizer.GPOptimizer` instance, an array of positions (of size V x D), and hyperparameters (a 1-D
        array of length D+1 for the default kernel). The return value is a 1-D array of length V. If None is provided,
        `fvgp.gp.GP.default_mean_function` is used.
    run_every_iteration : Callable, optional
        A function that is run at every iteration. It accepts as input this
        `gpcam.autonomous_experimenter.AutonomousExperimenterGP` instance. The default is a no-op.
    x_data : np.ndarray, optional
        Initial data point positions
    y_data : np.ndarray, optional
        Initial data point values
    variances : np.ndarray, optional
        Initial data point observation variances
    vp : np.ndarray, optional
        A 3-D numpy array of shape (U x output_number x output_dim), so that for each measurement position, the outputs
        are clearly defined by their positions in the output space. The default is np.array([[0],[1],[2],[3],...,[output_number - 1]]) for each
        point in the input space. The default is only permissible if output_dim is 1.
    communicate_full_dataset : bool, optional
        If True, the full dataset will be communicated to the `instrument_func` on each iteration. If False, only the
        newly suggested data points will be communicated. The default is False.
    compute_device : str, optional
        One of "cpu" or "gpu", determines how linear system solves are run. The default is "cpu".
    store_inv : bool, optional
        If True, the algorithm calculates and stores the inverse of the covariance matrix after each training or update of the dataset,
        which makes computing the posterior covariance faster.
        For larger problems (>2000 data points), the use of inversion should be avoided due to computational instability. The default is
        False. Note, the training will always use a linear solve instead of the inverse for stability reasons.
    training_dask_client : distributed.client.Client, optional
        A Dask Distributed Client instance for distributed training. If None is provided, a new
        `dask.distributed.Client` instance is constructed.
    acq_func_opt_dask_client : distributed.client.Client, optional
        A Dask Distributed Client instance for distributed `acquisition_func` computation. If None is provided, a new
        `dask.distributed.Client` instance is constructed.
    info : bool, optional
        bool specifying if the should be extensive std out. Default = False


    Attributes
    ----------
    x_data : np.ndarray
        Data point positions
    y_data : np.ndarray
        Data point values
    variances : np.ndarray
        Data point observation variances
    hyperparameter_bounds : np.ndarray
        A 2-D array of floats of size J x 2, such that J is the length matching the length of `hyperparameters` defining
        the bounds for training.
    gp_optimizer : gpcam.gp_optimizer.GPOptimizer
        A GPOptimizer instance used for initializing a gaussian process and performing optimization of the posterior.


    """

    def __init__(self,
                 input_space_bounds,
                 output_number,
                 output_dim,
                 hyperparameters,
                 hyperparameter_bounds,
                 instrument_func=None,
                 init_dataset_size=None,
                 acq_func="variance",
                 cost_function=None,
                 cost_update_function=None,
                 cost_function_parameters={},
                 kernel_func=None,
                 prior_mean_func=None,
                 noise_function = None,
                 run_every_iteration=None,
                 x_data=None, y_data=None, variances=None, vp=None, dataset=None,
                 communicate_full_dataset=False,
                 compute_device="cpu",
                 store_inv=False,
                 training_dask_client=None,
                 acq_func_opt_dask_client=None,
                 sparse_mode = False,
                 gp2Scale = False,
                 gp2Scale_dask_client = None,
                 gp2Scale_batch_size = 10000,
                 normalize_y = False,
                 ram_economy=True,
                 info=False,
                 args = None
                 ):


        dim = len(input_space_bounds)
        self.input_space_bounds = input_space_bounds
        self.instrument_func = instrument_func
        self.hyperparameters = hyperparameters
        self.hyperparameter_bounds = hyperparameter_bounds
        self.acq_func = acq_func
        self.run_every_iteration = run_every_iteration
        self.communicate_full_dataset = communicate_full_dataset
        self.async_train_in_progress = False
        self.training_dask_client = training_dask_client
        self.args = args

        if self.training_dask_client is None: self.training_dask_client = dask.distributed.Client()
        self.acq_func_opt_dask_client = acq_func_opt_dask_client
        if self.acq_func_opt_dask_client is None: self.acq_func_opt_dask_client = self.training_dask_client
        ################################
        # getting the data ready#########
        ################################
        if init_dataset_size is None and x_data is None and dataset is None:
            raise Exception("Either provide length of initial data or an inital dataset")
        self.data = fvgpData(dim, input_space_bounds,
                             output_number=output_number, output_dim=output_dim)
        if x_data is None and dataset is None:
            self.data.create_random_dataset(init_dataset_size)
            if instrument_func is None: raise Exception("You need to provide an instrument function.")
            self.data.dataset = self.instrument_func(self.data.dataset)
        elif dataset is not None:
            self.data.inject_dataset(list(np.load(dataset, allow_pickle=True)))
            self.hyperparameters = self.data.dataset[-1]["hyperparameters"]
        elif x_data is not None and y_data is not None:
            self.data.dataset = self.data.inject_arrays(x_data, y=y_data, v=variances, vp=vp)
        elif x_data is not None and y_data is None:
            if instrument_func is None: raise Exception("You need to provide an instrument function.")
            self.data.dataset = self.instrument_func(self.data.inject_arrays(x_data, y=y_data, v=variances))
        else:
            raise Exception("No viable option for data given!")
        self.data.check_incoming_data()
        if self.data.nan_in_dataset(): self.data.clean_data_NaN()
        self.x_data, self.y_data, self.noise_variances, self.times, self.costs, self.vp = self.data.extract_data()
        self.init_dataset_size = len(self.x_data)
        ######################
        ######################
        ######################
        self.gp_optimizer = fvGPOptimizer(
            self.x_data, self.y_data,
            output_space_dimension = output_dim,
            init_hyperparameters = hyperparameters,
            noise_variances = self.noise_variances,
            compute_device = compute_device,
            gp_kernel_function = kernel_func,
            gp_kernel_function_grad = None,
            gp_noise_function = noise_function,
            gp_noise_function_grad = None,
            gp_mean_function = prior_mean_func,
            gp_mean_function_grad = None,
            sparse_mode = sparse_mode,
            gp2Scale = gp2Scale,
            gp2Scale_dask_client = gp2Scale_dask_client,
            gp2Scale_batch_size = gp2Scale_batch_size,
            normalize_y = normalize_y,
            store_inv = store_inv,
            ram_economy = ram_economy,
            args = args,
            info = info,
            cost_function = cost_function,
            cost_function_parameters = cost_function_parameters,
            cost_update_function = cost_update_function)


        # init costs
        self.info = info
        if info:
            logger.info(inspect.cleandoc("""#
            ##################################################################################
            Autonomous Experimenter fvGP initialization successfully concluded
            now train(...) or train_async(...), and then go(...)
            ##################################################################################"""))

    def _extract_data(self):
        x, y, v, t, c, vp = self.data.extract_data()
        return x, y, v, t, c, vp

