from dataclasses import dataclass
import numpy as np
from typing import Dict, List, Tuple, Any
from scipy.stats import norm
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, ConstantKernel, Matern
from sim.envs.base.mujoco_env import MujocoEnv, MujocoCfg


@dataclass
class OptimizeParam:
    """Definition of a parameter to optimize"""
    name: str  # Full parameter path (e.g., 'gains.kp_scale')
    min_val: float
    max_val: float
    init_val: float = None


class BayesianOptimizer:
    def __init__(
        self,
        base_cfg: MujocoCfg,
        parameters: List[OptimizeParam],
        n_initial_points: int = 10,
        n_iterations: int = 40,
        n_episodes: int = 5,
        exploration_weight: float = 0.1
    ):
        self.base_cfg = base_cfg
        self.parameters = parameters
        self.n_initial_points = n_initial_points
        self.n_iterations = n_iterations
        self.n_episodes = n_episodes
        self.exploration_weight = exploration_weight
        
        # Initialize storage for observations
        self.X = []  # Parameter configurations
        self.y = []  # Observed scores
        
        # Set up parameter bounds for GP
        self.bounds = np.array([[p.min_val, p.max_val] for p in parameters])
        
        # Initialize GP with Matérn kernel (more flexible than RBF for real processes)
        kernel = ConstantKernel() * Matern(nu=2.5)
        self.gp = GaussianProcessRegressor(
            kernel=kernel,
            n_restarts_optimizer=10,
            normalize_y=True,
            random_state=42
        )

    def _get_cfg_value(self, cfg: MujocoCfg, param_path: str) -> Any:
        """Get a value from nested config using dot notation"""
        value = cfg
        for part in param_path.split('.'):
            value = getattr(value, part)
        return value

    def _set_cfg_value(self, cfg: MujocoCfg, param_path: str, value: Any) -> None:
        """Set a value in nested config using dot notation"""
        parts = param_path.split('.')
        obj = cfg
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)

    def _params_to_array(self, params: Dict[str, float]) -> np.ndarray:
        """Convert parameter dictionary to array"""
        return np.array([params[p.name] for p in self.parameters])

    def _array_to_params(self, x: np.ndarray) -> Dict[str, float]:
        """Convert array to parameter dictionary"""
        return {p.name: float(val) for p, val in zip(self.parameters, x)}

    def _sample_random_point(self) -> np.ndarray:
        """Sample a random point in the parameter space"""
        return np.array([
            np.random.uniform(p.min_val, p.max_val)
            for p in self.parameters
        ])

    def _expected_improvement(self, X: np.ndarray, xi: float = 0.01) -> np.ndarray:
        """Calculate expected improvement at points X"""
        # Ensure X is 2D
        X = X.reshape(-1, len(self.parameters))
        
        # Get predictive distribution from GP
        mu, sigma = self.gp.predict(X, return_std=True)
        
        # Get current best
        mu_best = np.max(self.y)
        
        # Calculate expected improvement
        with np.errstate(divide='warn'):
            imp = mu - mu_best - xi
            Z = imp / sigma
            ei = imp * norm.cdf(Z) + sigma * norm.pdf(Z)
            ei[sigma == 0.0] = 0.0
            
        return ei

    def _evaluate_parameters(self, params: Dict[str, float]) -> float:
        """Evaluate a parameter set over multiple episodes"""
        # Create a copy of base config and update with new parameters
        cfg = MujocoCfg()
        cfg.__dict__.update(self.base_cfg.__dict__)
        
        for param_name, value in params.items():
            self._set_cfg_value(cfg, param_name, value)
        
        env = MujocoEnv(cfg, render=False)
        rewards = []
        
        try:
            for _ in range(self.n_episodes):
                env.reset()
                episode_reward = 0
                episode_length = 0
                done = False
                
                while not done:
                    # Use sinusoidal actions for testing stability
                    t = episode_length * env.cfg.sim.dt
                    action = 0.5 * np.sin(2 * np.pi * t)
                    _, reward, done, info = env.step(action * np.ones(env.num_joints))
                    
                    episode_reward += reward
                    episode_length += 1
                    
                    # Early termination for unstable episodes
                    if info.get('fall', False):
                        episode_reward *= 0.5  # Penalty for falling
                        break
                    
                rewards.append(episode_reward)
            
            # Calculate score with stability consideration
            mean_reward = np.mean(rewards)
            std_reward = np.std(rewards)
            stability_penalty = 0.1 * std_reward
            
            return mean_reward - stability_penalty
            
        except Exception as e:
            print(f"Evaluation failed: {e}")
            return float('-inf')
        finally:
            env.close()

    def _next_point(self) -> np.ndarray:
        """Determine next point to evaluate using Thompson Sampling"""
        best_x = None
        best_ei = -float('inf')
        
        # Random search for point with highest expected improvement
        for _ in range(100):
            x = self._sample_random_point()
            ei = self._expected_improvement(x.reshape(1, -1))
            if ei > best_ei:
                best_ei = ei
                best_x = x
                
        return best_x

    def optimize(self) -> Tuple[Dict[str, float], List[float]]:
        """Run Bayesian optimization"""
        print("Starting Bayesian optimization...")
        print(f"Initial exploration: {self.n_initial_points} points")
        
        # Initial random exploration
        for i in range(self.n_initial_points):
            print(f"\nEvaluating initial point {i+1}/{self.n_initial_points}")
            x = self._sample_random_point()
            params = self._array_to_params(x)
            y = self._evaluate_parameters(params)
            
            self.X.append(x)
            self.y.append(y)
            
            print(f"Parameters: {params}")
            print(f"Score: {y:.2f}")
        
        # Convert lists to arrays
        self.X = np.array(self.X)
        self.y = np.array(self.y)
        
        # Main optimization loop
        best_score = max(self.y)
        best_params = self._array_to_params(self.X[np.argmax(self.y)])
        no_improvement_count = 0
        
        for iteration in range(self.n_iterations):
            print(f"\nIteration {iteration + 1}/{self.n_iterations}")
            
            # Fit GP to all data
            self.gp.fit(self.X, self.y)
            
            # Find next point to evaluate
            x = self._next_point()
            params = self._array_to_params(x)
            y = self._evaluate_parameters(params)
            
            # Update data
            self.X = np.vstack((self.X, x))
            self.y = np.append(self.y, y)
            
            # Update best score
            if y > best_score:
                best_score = y
                best_params = params
                no_improvement_count = 0
                print("New best configuration found!")
            else:
                no_improvement_count += 1
            
            print("Current parameters:")
            for name, value in params.items():
                print(f"  {name}: {value:.3f}")
            print(f"Score: {y:.2f}")
            print(f"Best score so far: {best_score:.2f}")
            
            # Early stopping
            if no_improvement_count >= 10:
                print("\nStopping early: No improvement for 10 iterations")
                break
        
        print("\nOptimization complete!")
        print("Best parameters found:")
        for name, value in best_params.items():
            print(f"{name}: {value:.3f}")
        print(f"Best score: {best_score:.2f}")
        
        return best_params, self.y.tolist()


if __name__ == "__main__":
    cfg = MujocoCfg()
    parameters = [
        OptimizeParam(
            name='gains.kp_scale',
            min_val=0.003,
            max_val=1.0,
        ),
        OptimizeParam(
            name='gains.kd_scale',
            min_val=1.00,
            max_val=70.0,
        ),
    ]
    
    optimizer = BayesianOptimizer(
        cfg,
        parameters,
        n_initial_points=10,
        n_iterations=200,
    )
    
    best_params, history = optimizer.optimize()