from concurrent.futures import ProcessPoolExecutor
import numpy as np
from functools import partial
import numpy as np

def generate_paths(S0, r, sigma, T, M, N):
    """
    Simulates underlying asset price paths for each asset with its own time-to-maturity.
        S0    : array of initial asset prices (length L)
    r     : array of risk-free rates (length L)
    sigma : array of volatilities (length L)
    T     : array of times to maturity (length L) or a scalar (in years)
    M     : number of time steps (so M+1 simulation points including time 0)
    N     : number of simulation paths

    Returns:
        paths: a NumPy array of shape (L, M+1, N)
    """
    S0 = np.atleast_1d(S0)
    r = np.atleast_1d(r)
    sigma = np.atleast_1d(sigma)
    T = np.atleast_1d(T)
    L = len(S0)

    # If T was provided as a scalar, broadcast it.
    if T.size == 1:
        T = np.full(L, T[0])

    # dt for each asset (shape: (L,))
    dt = T / M             

    # Reshape dt so it can be broadcast along time and simulation axes.
    dt_bc = dt[:, np.newaxis, np.newaxis]  # shape (L, 1, 1)

    # Generate normal variates for each asset, each time step and each path.
    Z = np.random.randn(L, M, N)

    # Compute log-increments for each asset.
    increments = (r[:, np.newaxis, np.newaxis] - 0.5 * sigma[:, np.newaxis, np.newaxis]**2) * dt_bc \
                + sigma[:, np.newaxis, np.newaxis] * np.sqrt(dt_bc) * Z
                
    # Cumulative sum gives log prices. Prepend zeros so that time0 is S0.
    logS = np.concatenate((np.zeros((L, 1, N)), np.cumsum(increments, axis=1)), axis=1)

    # Compute asset price paths.
    paths = S0[:, np.newaxis, np.newaxis] * np.exp(logS)
    return paths

def compute_nested_value(nested_paths, betas, K, r, T, M, t):
    """
    Compute nested simulation values for a single outer path
    """
    dt = T / M
    hv_nested = np.maximum(K - nested_paths, 0)
    nested_itm = hv_nested > 0
    
    # Compute continuation values for nested paths
    cv_nested = np.zeros_like(nested_paths)
    for ti in range(nested_paths.shape[0]):
        valid_paths = np.where(nested_itm[ti], nested_paths[ti], np.nan)
        cv_nested[ti] = np.polyval(betas[t+ti], valid_paths)
    
    # Find optimal exercise times
    exercise_signal = hv_nested > cv_nested
    t_hat = np.argmax(exercise_signal, axis=0)
    t_hat = np.where(np.any(exercise_signal, axis=0), t_hat, len(cv_nested) - 1)
    
    # Get payoffs at optimal exercise times
    hv_hat = np.array([hv_nested[t_hat[i], i] for i in range(nested_paths.shape[1])])
    
    # Discount payoffs
    discount = np.exp(-r * dt * (t_hat + t))
    discounted_payoff = hv_hat * discount
    
    return np.mean(discounted_payoff)

def process_time_step(t, curr_paths_t, betas, K, r, sigma, T, M, N_inner):
    """
    Process a single time step for all outer paths
    """
    dt = T / M
    df = np.exp(-r * dt * t)
    hv = np.maximum(K - curr_paths_t, 0)
    
    if t == M:
        return hv * df, hv
    
    # Generate nested paths for continuation value
    nested_paths = generate_paths(curr_paths_t, r, sigma, T - t*dt, M-t, N_inner)
    
    # Compute immediate exercise value
    immediate = np.maximum(K - curr_paths_t, 0)
    itm = immediate > 0
    
    # Compute continuation value
    cv = np.zeros_like(immediate)
    if np.any(itm):
        cv[itm] = np.polyval(betas[t], curr_paths_t[itm])
    
    # Determine exercise decision
    exercise = immediate > cv
    
    # Compute values
    Vti = np.zeros_like(immediate)
    Vti[exercise] = immediate[exercise] * df
    
    # Compute continuation values using nested simulation
    cont_values = np.array([
        compute_nested_value(
            nested_paths[:, :, i:i+1], 
            betas, K, r, T, M, t
        ) for i in range(nested_paths.shape[2])
    ])
    
    Vti[~exercise] = cont_values[~exercise]
    
    return Vti, hv

def dual_pricing_parallel(S0, K, r, sigma, T, M, N_outer, N_inner, betas, n_jobs=4):
    """
    Parallel implementation of dual pricing
    
    Parameters:
    -----------
    S0, K, r, sigma, T : as before
    M : number of time steps
    N_outer : number of outer paths
    N_inner : number of inner paths
    betas : regression coefficients from LSM
    n_jobs : number of parallel processes
    """
    # Generate all outer paths first
    outer_paths = generate_paths(S0, r, sigma, T, M, N_outer)
    L = len(S0)
    upper_bounds = np.zeros(L)
    
    for l in range(L):  # Process each asset separately
        curr_paths = outer_paths[l]  # Shape: (M+1, N_outer)
        
        # Initialize arrays
        martingales = np.zeros((N_outer, M + 1))
        hvs = np.zeros((N_outer, M + 1))
        
        # Set initial values
        hvs[:, 0] = np.maximum(K[l] - curr_paths[0], 0)
        
        # Create partial function with fixed parameters
        process_time_step_partial = partial(
            process_time_step,
            betas=betas[l],
            K=K[l],
            r=r[l],
            sigma=sigma[l],
            T=T[l],
            M=M,
            N_inner=N_inner
        )
        
        # Parallel processing of time steps
        with ProcessPoolExecutor(max_workers=n_jobs) as executor:
            # Prepare arguments for each time step
            time_steps = range(1, M + 1)
            curr_paths_t = [curr_paths[t] for t in time_steps]
            
            # Execute parallel computations
            results = list(executor.map(
                process_time_step_partial,
                time_steps,
                curr_paths_t
            ))
        
        # Process results and construct martingale
        for t, (Vti, hv) in zip(time_steps, results):
            hvs[:, t] = hv
            if t == 1:
                martingales[:, t] = Vti - np.mean(Vti)
            else:
                martingales[:, t] = martingales[:, t-1] + (Vti - np.mean(Vti))
        
        # Compute upper bound
        max_martingales = np.zeros((N_outer, M + 1))
        dt = T[l] / M
        discount_factors = np.exp(-r[l] * dt * np.arange(M + 1))
        
        # Compute upper bound process
        for t in range(M + 1):
            max_martingales[:, t] = hvs[:, t] * discount_factors[t] - martingales[:, t]
        
        upper_bounds[l] = np.mean(np.max(max_martingales, axis=1))
    
    return upper_bounds

def compute_exercise_decision(immediate_value, continuation_value, threshold=1e-10):
    """More robust exercise decision with numerical tolerance"""
    exercise = immediate_value > continuation_value + threshold
    return exercise

def compute_continuation_value(paths, betas, t, K, r, T, M):
    """Separate function for continuation value calculation"""
    itm = paths > 0
    continuation = np.zeros_like(paths)
    
    if np.any(itm):
        poly = np.poly1d(betas[t])
        continuation[itm] = np.polyval(poly, paths[itm])
        # Add boundary conditions
        continuation = np.maximum(continuation, 0)  # Non-negative continuation value
        continuation = np.minimum(continuation, K)  # Upper bound at strike price
    
    return continuation

def compute_upper_bound(hvs, martingales, df):
    """More efficient upper bound computation"""
    N = hvs.shape[0]
    M = hvs.shape[1] - 1
    
    # Vectorized computation of discounted payoffs
    discounted_hvs = hvs * df.reshape(1, -1)
    
    # Compute upper bound process
    upper_bound_process = np.zeros((N, M+1))
    for t in range(M+1):
        upper_bound_process[:, t] = discounted_hvs[:, t] - martingales[:, t]
    
    # Take minimum over stopping times
    upper_bound = np.min(upper_bound_process, axis=1)
    
    return np.mean(upper_bound)

def discount_factor(r, T, M, curr_time):
    """
    Computes the discount factor for a given risk-free rate and time to maturity.
    r : array of risk-free rates (length L)
    T : array of times to maturity (length L) or scalar (in years)
    M : number of time steps (without the initial time point)
    """
    dt = T / M  # Time increment for each step
    return np.exp(-r * dt * curr_time)  # Discount factor for each step