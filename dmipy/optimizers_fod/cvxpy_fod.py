import numpy as np
import cvxpy
from dipy.data import get_sphere, HemiSphere
from dipy.reconst.shm import real_sym_sh_mrtrix


__all__ = [
    'MultiCompartmentCSDOptimizer'
]


class MultiCompartmentCSDOptimizer:
    """
    General purpose optimizer for multi-compartment constrained spherical
    deconvolution (MC-CSD) to estimate Fiber Orientation Distributions (FODs).
    It can take any number of compartment models as convolution kernels as long
    as all the kernel's parameters are fixed. If more than one kernel is given,
    then the optimizer estimates the volume fractions of the different kernels
    as well as the FOD.

    The optimization package CVXPY [1]_ is used for this MC-CSD implementation.

    Limitations: It cannot estimate the FOD of multiple kernels that each have
    an orientation. E.g. it is possible to have a cylinder and a ball kernel
    as input, but not two cylinders.

    IMPORTANT NOTE: This multi-compartment CSD implementation is NOT the same
    Multi-Tissue CSD (MT-CSD) as proposed by Jeurissen et al. [2]_. In MT-CSD
    they input tissue response kernels that INCLUDES the differences in
    b0-intensities between different tissue types. In this current
    implementation of MC-CSD, the different compartment kernels are directly
    fitted to the signal attenuation, implicitly assuming each compartment has
    the same b0-intensity.

    Parameters
    ----------
    acquisition_scheme: DmipyAcquisitionScheme instance,
        acquisition scheme of the to-be-fitted data.
    model: dmipy MultiCompartmentModel instance,
        Can be composed of any model combination.
    sh_order: even integer larger or equal to zero,
        maximum spherical harmonics order to be included in the FOD estimation.
    unity_constrain: bool,
        whether or not to impose that the FOD integrates to unity.

    References
    ----------
    .. [1] Diamond, Steven, and Stephen Boyd. "CVXPY: A Python-embedded
        modeling language for convex optimization." The Journal of Machine
        Learning Research 17.1 (2016): 2909-2913.
    .. [2] Jeurissen, Ben, et al. "Multi-tissue constrained spherical
        deconvolution for improved analysis of multi-shell diffusion MRI data."
        NeuroImage 103 (2014): 411-426.
    """

    def __init__(self, acquisition_scheme, model, sh_order=8,
                 unity_constraint=True):
        self.model = model
        self.acquisition_scheme = acquisition_scheme
        self.sh_order = sh_order
        self.Ncoef = int((sh_order + 2) * (sh_order + 1) // 2)
        self.Nmodels = len(self.model.models)
        self.unity_constraint = unity_constraint

        # prepare positivity grid on sphere
        sphere = get_sphere('symmetric724')
        hemisphere = HemiSphere(phi=sphere.phi, theta=sphere.theta)
        self.sh_matrix_positivity = real_sym_sh_mrtrix(
            self.sh_order, hemisphere.theta, hemisphere.phi)[0]

        orientation_counter = 0
        for model in self.model.models:
            if 'orientation' in model.parameter_types.values():
                orientation_counter += 1
        if orientation_counter > 1:
            msg = 'Cannot optimize the volume fractions of multiple models '
            msg += 'with an orientation at the same time.'
            raise ValueError(msg)

    def __call__(self, data, x0_vector):
        """
        Estimates the FOD and possible volume fractions given the measured data
        and possibly fixed parameters.

        Parameters
        ----------
        data: array of size (Ndata,),
            array containing the measured signal attenuation.
        x0_vector: array of size (Nparameters,),
            initial guess array that either contains a float for parameters
            with an initial guess or None for parameters that have no guess.

        Returns
        -------
        fitted_parameter_vector: array of size (Nparameters,),
            array of the optimized model parameters.
        """

        # step 1: determine rotational_harmonics of kernel
        rh_matrix = self.recover_rotational_harmonics(x0_vector)

        # step 2: set up cvxpy problem
        volume_fraction_sum = 0.
        volume_fractions = []
        constraints = []
        cvxpy_variables = []
        for model in self.model.models:
            if 'orientation' in model.parameter_types.values():
                sh_coef = cvxpy.Variable(self.Ncoef)
                cvxpy_variables.append(sh_coef)
                volume_fraction = sh_coef[0] * (2 * np.sqrt(np.pi))
                volume_fractions.append(volume_fraction)
                volume_fraction_sum += volume_fraction
                constraints.append(self.sh_matrix_positivity * sh_coef > 0.)
            else:
                c00_coef = cvxpy.Variable(1)
                cvxpy_variables.append(c00_coef)
                volume_fraction = c00_coef[0] * (2 * np.sqrt(np.pi))
                volume_fractions.append(volume_fraction)
                volume_fraction_sum += volume_fraction
                constraints.append(c00_coef > 0.)

        # step 3: for every shell add the sum-squared error of the prediction.
        sse = 0.
        scheme = self.acquisition_scheme
        for i, shell_index in enumerate(scheme.unique_dwi_indices):
            shell_mask = scheme.shell_indices == shell_index
            shell_data = data[shell_mask]
            rh_order = int(scheme.shell_sh_orders[shell_index])
            sh_mat = scheme.shell_sh_matrices[shell_index]

            shell_data_predicted = 0.
            for j, model in enumerate(self.model.models):
                if 'orientation' in model.parameter_types.values():
                    sh_coef = cvxpy_variables[j]
                    rh_shell_prepared = self.prepare_rotational_harmonics(
                        rh_matrix[j][i], rh_order)
                    if self.sh_order >= rh_order:
                        sh_shell = (
                            cvxpy.diag(rh_shell_prepared) *
                            sh_coef[:len(rh_shell_prepared)])
                        shell_data_predicted += sh_mat * sh_shell
                    else:
                        sh_shell = (cvxpy.diag(
                            rh_shell_prepared[:self.Ncoef]) * sh_coef)
                        shell_data_predicted += sh_mat[:, :self.Ncoef] * \
                            sh_shell
                else:
                    c00_coef = cvxpy_variables[j]
                    sh_mat = scheme.shell_sh_matrices[shell_index][0, 0]
                    rh_order = 0
                    rh_shell_prepared = self.prepare_rotational_harmonics(
                        rh_matrix[j][i], rh_order)
                    sh_shell = rh_shell_prepared * c00_coef
                    shell_data_predicted += sh_mat * sh_shell
            sse += cvxpy.sum_squares(shell_data_predicted - shell_data)

        objective = cvxpy.Minimize(sse)
        if self.unity_constraint:
            constraints.append(volume_fraction_sum == 1.)
        problem = cvxpy.Problem(objective, constraints)
        problem.solve()

        fitted_params = self.model.parameter_vector_to_parameters(x0_vector)
        try:
            fitted_params['sh_coeff'] = np.array(sh_coef.value).squeeze()
        except NameError:
            pass
        if self.Nmodels > 1:
            fractions = [fraction.value for fraction in volume_fractions]
            fractions_array = np.array(fractions).squeeze()
            for i, name in enumerate(self.model.partial_volume_names):
                fitted_params[name] = fractions_array[i]
        fitted_parameter_vector = self.model.parameters_to_parameter_vector(
            **fitted_params)
        return fitted_parameter_vector

    def prepare_rotational_harmonics(self, rh_array, rh_order):
        "Function to extend rotational harmonics and prepare them for MSE."
        rh_coef = np.zeros(int((rh_order + 2) * (rh_order + 1) // 2))
        counter = 0
        for n_ in range(0, rh_order + 1, 2):
            coef_in_order = 2 * n_ + 1
            rh_coef[counter: counter + coef_in_order] = (
                rh_array[n_ // 2] * np.sqrt((4 * np.pi) / (2 * n_ + 1)))
            counter += coef_in_order
        return rh_coef

    def recover_rotational_harmonics(self, x0_vector):
        "Recovers list of rotational harmonics for each model."
        parameters = self.model.parameter_vector_to_parameters(x0_vector)
        parameters = self.model.add_linked_parameters_to_parameters(parameters)
        rh_models = []
        for model_name, model in zip(
            self.model.model_names, self.model.models
        ):
            model_parameters = {}
            for parameter in model.parameter_ranges:
                parameter_name = self.model._inverted_parameter_map[
                    (model, parameter)
                ]
                model_parameters[parameter] = parameters.get(
                    parameter_name
                )
            rh_model = model.rotational_harmonics_representation(
                self.acquisition_scheme, **model_parameters)
            rh_models.append(rh_model)
        return rh_models
