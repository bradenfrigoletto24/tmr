"""
This script performs mass minimization with natural frequency constraint
"""

# Import analysis-related libraries
from tmr import TMR, TopOptUtils
from paropt import ParOpt
from tacs import TACS, constitutive

# Import general-purpose libraries
import openmdao.api as om
import numpy as np
from mpi4py import MPI
import argparse
import os
import sys
import pickle

# Import utility classes and functions
from utils_freq import create_problem, ReducedProblem, getFixedDVIndices, ReduOmAnalysis

sys.path.append('../eigenvalue')
from utils import create_forest, getNSkipUpdate

if __name__ == '__main__':

    # Create the argument parser
    p = argparse.ArgumentParser()

    # os
    p.add_argument('--prefix', type=str, default='./results')

    # Analysis
    p.add_argument('--domain', type=str, default='cantilever',
        choices=['cantilever', 'michell', 'mbb', 'lbracket'])
    p.add_argument('--AR', type=float, default=1.0)
    p.add_argument('--ratio', type=float, default=0.4)
    p.add_argument('--len0', type=float, default=1.0)
    p.add_argument('--vol-frac', type=float, default=1.0)
    p.add_argument('--r0-frac', type=float, default=0.05)
    p.add_argument('--htarget', type=float, default=1.0)
    p.add_argument('--mg-levels', type=int, default=4)
    p.add_argument('--qval', type=float, default=5.0)
    p.add_argument('--lambda0', type=float, default=0.1)
    p.add_argument('--ksrho', type=float, default=1000)

    p.add_argument('--eig-method', type=str, default='jd', choices=['jd', 'lanczos'])
    p.add_argument('--max-jd-size', type=int, default=200)
    p.add_argument('--max-gmres-size', type=int, default=30)
    p.add_argument('--num-eigenvalues', type=int, default=10)
    p.add_argument('--max-lanczos', type=int, default=30)
    p.add_argument('--jd-recycle', type=str, default='on', choices=['on', 'off'])
    p.add_argument('--lanczos-shift', type=float, default=-10.0)
    p.add_argument('--jd-use-Amat-shift', type=str, default='on', choices=['on', 'off'])

    # Optimization
    p.add_argument('--optimizer', type=str, default='paropt',
        choices=['paropt', 'snopt', 'ipopt', 'mma'])
    p.add_argument('--n-mesh-refine', type=int, default=1)
    p.add_argument('--max-iter', type=int, default=100)
    p.add_argument('--qn-correction', action='store_true')
    p.add_argument('--fixed-mass', type=float, default=1.0,
        help='value for fixed nodal design variables, must be within [0, 1]')
    p.add_argument('--eig-scale', type=float, default=1.0)
    p.add_argument('--output-level', type=int, default=0)
    p.add_argument('--simple-filter', action='store_false')
    p.add_argument('--tr-eta', type=float, default=0.25)
    p.add_argument('--tr-min', type=float, default=1e-3)
    p.add_argument('--eq-constr', action='store_true')
    p.add_argument('--qn-subspace', type=int, default=10)
    p.add_argument('--qn-type', type=str, default='scaled_bfgs', choices=['bfgs', 'scaled_bfgs'])
    p.add_argument('--paropt-type', type=str, default='penalty_method',
        choices=['penalty_method', 'filter_method'])

    # Test
    p.add_argument('--gradient-check', action='store_true')

    # Parse arguments
    args = p.parse_args()

    mg_levels = args.mg_levels
    prefix = args.prefix

    if args.jd_recycle == 'on':
        jd_use_recycle = True
    else:
        jd_use_recycle = False

    if args.jd_use_Amat_shift == 'on':
        jd_use_Amat_shift = True
    else:
        jd_use_Amat_shift = False

    # Set the communicator
    comm = MPI.COMM_WORLD

    # Create prefix directory if not exist
    if comm.rank == 0 and not os.path.isdir(prefix):
        os.mkdir(prefix)

    # Save the command and arguments that executed this script
    if comm.rank == 0:
        cmd = 'python ' + ' '.join(sys.argv)
        with open(os.path.join(prefix,'exe.sh'), 'w') as f:
            f.write(cmd + '\n')

    # Allow root processor some time to save execution command
    comm.Barrier()

    # Compute derived geometry parameters
    lx = args.len0*args.AR
    ly = args.len0
    lz = args.len0
    if args.domain == 'lbracket':
        ly = args.len0*args.ratio

    # Set up material properties
    material_props = constitutive.MaterialProperties(rho=2600.0, E=70e3, nu=0.3, ys=100.0)

    # Create stiffness properties
    stiffness_props = TMR.StiffnessProperties(material_props, k0=1e-3, eps=0.2,
        q=args.qval, qmass=args.qval) # Try larger q val: 8, 10, 20, using same penalty for mass

    # Create initial forest
    forest = create_forest(comm, lx, ly, lz, args.ratio, args.htarget, mg_levels-1, args.domain)

    # Set boundary conditions
    bcs = TMR.BoundaryConditions()
    if args.domain == 'mbb':
        bcs.addBoundaryCondition('symmetry', [0], [0.0])
        bcs.addBoundaryCondition('support', [1,2], [0.0, 0.0])
    else:
        bcs.addBoundaryCondition('fixed', [0,1,2], [0.0, 0.0, 0.0])

    # Set up ParOpt parameters
    if args.paropt_type == 'penalty_method':
        adaptive_gamma_update = True
    else:
        adaptive_gamma_update = False
    optimization_options = {
        'algorithm': 'tr',
        'output_level':args.output_level,
        'norm_type': 'l1',
        'tr_init_size': 0.05,
        'tr_min_size': args.tr_min,
        'tr_max_size': 1.0,
        'tr_eta': args.tr_eta,
        'tr_infeas_tol': 1e-6,
        'tr_l1_tol': 0.0,
        'tr_linfty_tol': 0.0,
        'tr_adaptive_gamma_update': adaptive_gamma_update,
        'tr_accept_step_strategy': args.paropt_type,
        'filter_sufficient_reduction': args.simple_filter,
        'filter_has_feas_restore_phase': True,
        'tr_use_soc': False,
        'tr_max_iterations': args.max_iter,
        'penalty_gamma': 50.0,
        'qn_subspace_size': args.qn_subspace, # try 5 or 10
        'qn_type': args.qn_type,
        'qn_diag_type': 'yty_over_yts',
        'abs_res_tol': 1e-8,
        'starting_point_strategy': 'affine_step',
        'barrier_strategy': 'mehrotra_predictor_corrector',
        'tr_steering_barrier_strategy': 'mehrotra_predictor_corrector',
        'tr_steering_starting_point_strategy': 'affine_step',
        'use_line_search': False,  # subproblem
        'max_major_iters': 200}

    mma_options = {
        'algorithm': 'mma',
        'mma_asymptote_contract': 0.7,
        'mma_asymptote_relax': 1.2,
        'mma_bound_relax': 0,
        'mma_delta_regularization': 1e-05,
        'mma_eps_regularization': 0.001,
        'mma_infeas_tol': 1e-05,
        'mma_init_asymptote_offset': 0.25,
        'mma_l1_tol': 1e-06,
        'mma_linfty_tol': 1e-06,
        'mma_max_asymptote_offset': 10,
        'mma_max_iterations': args.max_iter,
        'mma_min_asymptote_offset': 0.01,
        'mma_use_constraint_linearization': True
    }

    # Set the original filter to NULL
    old_filter = None

    # Number of mesh refinements we have
    n_refine_steps = args.n_mesh_refine

    # This is the mesh refinement loop: each step corresponds to a refined mesh
    # except for the first step where the original (coarse) mesh is used
    for step in range(n_refine_steps):
        # Compute the offset
        iter_offset = step*args.max_iter

        # Create the optimization problem
        problem, obj_callback, constr_callback = create_problem(prefix=args.prefix, domain=args.domain,
                                                 forest=forest, bcs=bcs,
                                                 props=stiffness_props, nlevels=mg_levels+step,
                                                 lambda0=args.lambda0, ksrho=args.ksrho,
                                                 vol_frac=args.vol_frac,
                                                 r0_frac=args.r0_frac,
                                                 len0=args.len0, AR=args.AR, ratio=args.ratio,
                                                 iter_offset=iter_offset,
                                                 eig_scale=args.eig_scale,
                                                 eq_constr=args.eq_constr,
                                                 num_eigenvalues=args.num_eigenvalues,
                                                 eig_method=args.eig_method,
                                                 max_jd_size=args.max_jd_size,
                                                 jd_use_recycle=jd_use_recycle,
                                                 max_gmres_size=args.max_gmres_size,
                                                 max_lanczos=args.max_lanczos,
                                                 lanczos_shift=args.lanczos_shift,
                                                 jd_use_Amat_shift=jd_use_Amat_shift)

        # Function handle for qn correction, if specified
        if args.qn_correction:
            qn_corr_func = constr_callback.qn_correction
        else:
            qn_corr_func = None

        # Set the prefix
        problem.setPrefix(prefix)

        # Initialize the problem, set conuter and the prefix
        problem.initialize()
        problem.setIterationCounter(iter_offset)

        if args.gradient_check:
            for i in range(3):
                problem.checkGradients(1e-6)
            exit(0)

        # Extract the filter to interpolate design variables
        new_filter = problem.getFilter()

        # Create a reduced problem by fixing design variables where a constant mass
        # need to be applied to
        fixed_dv_idx = getFixedDVIndices(forest, args.domain, args.len0, args.AR, args.ratio)
        redu_prob = ReducedProblem(problem, fixed_dv_idx, fixed_dv_val=args.fixed_mass,
            qn_correction_func=qn_corr_func)

        # Create a reduced x0
        redu_x0 = redu_prob.createDesignVec()

        # Helper variable: a full-sized design vector
        _x = problem.createDesignVec()
        _xopt = problem.createDesignVec()

        if step != 0:  # This is a refined step - interpolation needed
            # Populate _xopt
            redu_prob.reduDVtoDV(redu_xopt, _xopt)

            # Populate _x
            TopOptUtils.interpolateDesignVec(old_filter, _xopt, new_filter, _x)

            # copy values from _x to redu_x0
            redu_prob.DVtoreduDV(_x, redu_x0)

        else:  # This is the first step, we just set x0 to 0.95
            redu_x0[:] = 0.95

        # Update filter
        old_filter = new_filter

        # Adjust maxiter for optimizer if this is the last refined step
        if n_refine_steps > 1:
            if step == n_refine_steps - 1:
                optimization_options['tr_max_iterations'] = 15
                mma_options['mma_max_iterations'] = 15

        # Set output path
        optimization_options['output_file'] = os.path.join(prefix, 'output_file%d.dat'%(step))
        optimization_options['tr_output_file'] = os.path.join(prefix, 'tr_output_file%d.dat'%(step))
        mma_options['mma_output_file'] = os.path.join(prefix, 'mma_output_file%d.dat'%(step))

        # Allocate space to store xopt
        redu_xopt = redu_prob.createDesignVec()

        # Optimize with openmdao/pyoptsparse wrapper if specified
        if args.optimizer == 'snopt' or args.optimizer == 'ipopt':

            # Create distributed openMDAO component
            omprob = om.Problem()
            analysis = ReduOmAnalysis(comm, redu_prob, redu_x0)
            indeps = omprob.model.add_subsystem('indeps', om.IndepVarComp())

            # Create global design vector
            redu_x0_g = comm.allgather(np.array(redu_x0))
            redu_x0_g = np.concatenate(redu_x0_g)
            indeps.add_output('x', redu_x0_g)
            omprob.model.add_subsystem('topo', analysis)
            omprob.model.connect('indeps.x', 'topo.x')
            omprob.model.add_design_var('indeps.x', lower=0.0, upper=1.0)
            omprob.model.add_objective('topo.obj')
            if args.eq_constr:
                omprob.model.add_constraint('topo.con', lower=0.0, upper=0.0)
            else:
                omprob.model.add_constraint('topo.con', lower=0.0)

            # Set up optimizer and options
            if args.optimizer == 'snopt':
                omprob.driver = om.pyOptSparseDriver()
                omprob.driver.options['optimizer'] = 'SNOPT'
                omprob.driver.opt_settings['Iterations limit'] = 9999999999999
                omprob.driver.opt_settings['Major feasibility tolerance'] = 1e-10
                omprob.driver.opt_settings['Major optimality tolerance'] = 1e-10
                omprob.driver.opt_settings['Summary file'] = os.path.join(prefix, 'snopt_output_file%d.dat'%(step))
                omprob.driver.opt_settings['Print file'] = os.path.join(prefix, 'print_output_file%d.dat'%(step))
                omprob.driver.opt_settings['Major print level'] = 1
                omprob.driver.opt_settings['Minor print level'] = 0

                if n_refine_steps > 1 and step == n_refine_steps - 1:
                    omprob.driver.opt_settings['Major iterations limit'] = 15
                else:
                    omprob.driver.opt_settings['Major iterations limit'] = args.max_iter

            elif args.optimizer == 'ipopt':
                omprob.driver = om.pyOptSparseDriver()
                omprob.driver.options['optimizer'] = 'IPOPT'
                omprob.driver.opt_settings['tol'] = 1e-10
                omprob.driver.opt_settings['constr_viol_tol'] = 1e-10
                omprob.driver.opt_settings['dual_inf_tol'] = 1e-10
                omprob.driver.opt_settings['output_file'] = os.path.join(prefix, 'ipopt_output_file%d.dat'%(step))

                if n_refine_steps > 1 and step == n_refine_steps - 1:
                    omprob.driver.opt_settings['max_iter'] = 15
                else:
                    omprob.driver.opt_settings['max_iter'] = args.max_iter

            omprob.setup()

            # Optimize and write result to f5 file
            omprob.run_driver()

            # Get optimal result from root processor and broadcast
            if comm.rank == 0:
                redu_xopt_g = omprob.get_val('indeps.x')  # Global vector
            else:
                redu_xopt_g = None
            redu_xopt_g = comm.bcast(redu_xopt_g, root=0)

            # Create a distributed vector and store the optimal solution
            # to hot-start the optimization on finer mesh
            analysis.globalVecToLocalvec(redu_xopt_g, redu_xopt)

            # Compute data of interest
            discreteness = np.dot(redu_xopt_g, 1.0-redu_xopt_g) / len(redu_xopt_g)
            obj = omprob.get_val('topo.obj')[0]
            con = omprob.get_val('topo.con')[0]

        # Otherwise, use ParOpt.Optimizer to optimize
        else:
            if args.optimizer == 'mma':
                # opt = ParOpt.Optimizer(problem, mma_options)
                opt = ParOpt.Optimizer(redu_prob, mma_options)
            else:
                # opt = ParOpt.Optimizer(problem, optimization_options)
                opt = ParOpt.Optimizer(redu_prob, optimization_options)
            opt.optimize()
            redu_xopt, z, zw, zl, zu = opt.getOptimizedPoint()

            # Get optimal objective and constraint
            fail, obj, cons = redu_prob.evalObjCon(redu_xopt)
            con = cons[0]

            # Compute discreteness
            redu_xopt_g = comm.allgather(np.array(redu_xopt))
            redu_xopt_g = np.concatenate(redu_xopt_g)
            discreteness = np.dot(redu_xopt_g, 1.0-redu_xopt_g) / len(redu_xopt_g)

        # Manually create the f5 file
        flag = (TACS.OUTPUT_CONNECTIVITY |
                TACS.OUTPUT_NODES |
                TACS.OUTPUT_DISPLACEMENTS |
                TACS.OUTPUT_EXTRAS)
        f5 = TACS.ToFH5(problem.getAssembler(), TACS.SOLID_ELEMENT, flag)
        f5.writeToFile(os.path.join(args.prefix, 'output_refine{:d}.f5'.format(step)))

        # Compute infeasibility
        if args.eq_constr:
            infeas = np.abs(con)
        else:
            infeas = np.max([-con, 0])

        # Export data to python pickle file
        if comm.rank == 0:

            # Check data
            print('[Optimum] discreteness:{:20.10e}'.format(discreteness))
            print('[Optimum] obj:         {:20.10e}'.format(obj))
            print('[Optimum] con:         {:20.10e}'.format(con))
            print('[Optimum] infeas:      {:20.10e}'.format(infeas))

            pkl = dict()
            pkl['discreteness'] = discreteness
            pkl['obj'] = obj
            pkl['con'] = con
            pkl['infeas'] = infeas
            pkl['domain'] = args.domain
            pkl['AR'] = args.AR
            pkl['ratio'] = args.ratio
            pkl['len0'] = args.len0
            pkl['r0-frac'] = args.r0_frac
            pkl['htarget'] = args.htarget
            pkl['mg-levels'] = args.mg_levels
            pkl['qval'] = args.qval
            pkl['max-jd-size'] = args.max_jd_size
            pkl['max-gmres-size'] = args.max_gmres_size
            pkl['optimizer'] = args.optimizer
            pkl['n-mesh-refine'] = args.n_mesh_refine
            pkl['max-iter'] = args.max_iter
            pkl['qn-correction'] = args.qn_correction
            pkl['eig-scale'] = args.eig_scale
            pkl['eq-constr'] = args.eq_constr
            pkl['qn-subspace'] = args.qn_subspace
            pkl['cmd'] = cmd
            pkl['problem'] = 'frequency'
            pkl['paropt-type'] = args.paropt_type

            if args.optimizer == 'paropt':
                pkl['curvs'] = constr_callback.getQnUpdateCurvs()
                pkl['n_skipH'] = getNSkipUpdate(os.path.join(prefix, 'tr_output_file%d.dat'%(step)))

            with open(os.path.join(prefix, 'output_refine%d.pkl'%(step)), 'wb') as f:
                pickle.dump(pkl, f)

        # Output for visualization
        assembler = problem.getAssembler()
        forest = forest.duplicate()

        # If not the final step, refine and repartition the mesh
        density_based_refine = False  # Hard-coded - don't use density based refine
        if step != n_refine_steps-1:
            if density_based_refine:
                # Refine based solely on the value of the density variable
                TopOptUtils.densityBasedRefine(forest, assembler, lower=0.05, upper=0.5)
            else:
                # Perform refinement based on distance
                dist_file = os.path.join(prefix, 'distance_solution%d.f5'%(step))

                # Compute the characteristic domain length
                vol = lx*ly*lz
                domain_length = vol**(1.0/3.0)
                refine_distance = 0.025*domain_length
                TopOptUtils.approxDistanceRefine(forest, new_filter, assembler, refine_distance,
                                                domain_length=domain_length,
                                                filename=dist_file)

            # Repartition the mesh
            forest.balance(1)
            forest.repartition()
