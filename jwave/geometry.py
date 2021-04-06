from jax import numpy as jnp
from functools import reduce
import jax
from typing import NamedTuple, Tuple, List
import numpy as np


class kGrid(NamedTuple):
    r"""
    A reference grid object for spatial and spectral calculations

    Attributes:
        N (Tuple[int]): grid dimensions
        dx (Tuple[int]): spatial sampling
        k_vec (List[jnp.ndarray]):
        k_staggered (dict):
        k_with_kspaceop (dict):
        space_axis (List[jnp.ndarray]):

    """
    N: Tuple[int]
    dx: Tuple[float]
    k_vec: List[jnp.ndarray]
    cell_area: float
    k_staggered: dict
    k_with_kspaceop: dict
    space_axis: List[jnp.ndarray]

    @property
    def domain_size(self):
        r"""Returns the lenght of the grid sides

        !!! example
            ```python
            L = grid.domain_size
            ```

        """
        return list(map(lambda x, y: x * y, zip(self.N, self.dx)))

    @staticmethod
    def make_grid(N, dx):
        r"""Constructs a `kGrid` object.

        Args:
                N (tuple(int)): The number of gridpoints per axis
                dx (tuple(int)): The sampling interval for each axis

        !!! example
            ```python
            grid = kGrid.make_grid(N=(64,64), dx=(0.3, 0.3))
            ```

        """

        def spatial_axis(n, delta):
            if n % 2 == 0:
                return jnp.arange(0, n) * delta - delta * n / 2
            else:
                return jnp.arange(0, n) * delta - delta * (n - 1) / 2

        axis = [spatial_axis(n, delta) for n, delta in zip(N, dx)]

        def k_axis(n, d):
            return jnp.fft.fftfreq(n, d) * 2 * jnp.pi

        k_vec = [k_axis(n, delta) for n, delta in zip(N, dx)]
        cell_area = reduce(lambda x, y: x * y, dx)

        return kGrid(
            N=N,
            dx=dx,
            k_vec=k_vec,
            cell_area=cell_area,
            k_staggered=None,
            k_with_kspaceop=None,
            space_axis=axis,
        )

    def to_staggered(self):
        r"""Produces a copy of the grid with the staggered vectors field set

        !!! example
            ```python
            grid = kGrid.make_grid(N=(64,64), dx=(0.3, 0.3))
            grid = grid.to_staggered()
            ```

        """
        tuple_to_dict = self._asdict()
        tuple_to_dict["k_staggered"] = {
            "backward": list(
                map(
                    lambda x: x[0] * jnp.exp(1j * x[0] * x[1] / 2),
                    zip(self.k_vec, self.dx),
                )
            ),
            "forward": list(
                map(
                    lambda x: x[0] * jnp.exp(-1j * x[0] * x[1] / 2),
                    zip(self.k_vec, self.dx),
                )
            ),
        }
        return kGrid(**tuple_to_dict)

    def apply_kspace_operator(self, c_ref, dt):
        r"""Modifies the k-vectors used for derivative calculation
        by applying the k-space operator (see [k-Wave](www.k-wave.org))

        !!! warning
                It makes sense to use those vectors **only to calculate
                first order derivatives**. This is a bad design choice,
                but makes things faster than calculating the operator
                each time.

        Args:
                c_ref (float): Reference speed of sound
                dt (float): Temporal stepsize

        Returns:
                grid: An updated grid containing the modified k-vectors under the
                        key `"kgrid_with_kspace_op"`

        !!! example
            ```python
            grid = kGrid.make_grid(N=(64,64), dx=(0.3, 0.3))
            grid = grid.to_staggered()
            grid = grid.apply_kspace_operator(c_ref = 1480, dt = time_array.dt)
            ```

        """
        assert self.k_staggered
        tuple_to_dict = self._asdict()

        K = jnp.stack(jnp.meshgrid(*self.k_vec, indexing="ij"))
        # k_magnitude = jnp.sqrt(jnp.sum(K ** 2, 0))

        # TODO: Check why it seems to work better without k_space_op
        k_space_op = 1.0  # safe_sinc(c_ref * k_magnitude * dt/(2*jnp.pi))
        modified_kgrid = jax.tree_util.tree_map(lambda x: x * k_space_op, K)

        # Making staggered versions
        K = jnp.stack(jnp.meshgrid(*self.k_staggered["backward"], indexing="ij"))
        modified_kgrid_backward = jax.tree_util.tree_map(lambda x: x * k_space_op, K)
        K = jnp.stack(jnp.meshgrid(*self.k_staggered["forward"], indexing="ij"))
        modified_kgrid_forward = jax.tree_util.tree_map(lambda x: x * k_space_op, K)

        # Update grid
        tuple_to_dict["k_with_kspaceop"] = {
            "plain": modified_kgrid,
            "backward": modified_kgrid_backward,
            "forward": modified_kgrid_forward,
        }
        return kGrid(**tuple_to_dict)


class Medium(NamedTuple):
    r"""
    Medium structure

    Attributes:
        sound_speed (jnp.darray): speed of sound map, can be a scalar
        density (jnp.ndarray): density map, can be a scalar
        attenuation (jnp.ndarray): attenuation map, can be a scalar
        pml_size (int): size of the PML layer in grid-points


    !!! example
        ```python
        N = (128,356)
        medium = Medium(
            sound_speed = jnp.ones(N),
            density = jnp.ones(N),
            attenuation = 0.0,
            pml_size = 15
        )
        ```

    """
    sound_speed: jnp.ndarray
    density: jnp.ndarray
    attenuation: jnp.ndarray
    pml_size: int


def _points_on_circle(n, radius, centre, cast_int=True, angle=0.0):
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False)
    x = (radius * np.cos(angles + angle) + centre[0]).tolist()
    y = (radius * np.sin(angles + angle) + centre[1]).tolist()
    if cast_int:
        x = list(map(int, x))
        y = list(map(int, y))
    return x, y


def _circ_mask(N, radius, centre):
    x, y = np.mgrid[0 : N[0], 0 : N[1]]
    dist_from_centre = np.sqrt((x - centre[0]) ** 2 + (y - centre[1]) ** 2)
    mask = (dist_from_centre < radius).astype(int)
    return mask


class Sources(NamedTuple):
    r"""Sources structure

    Attributes:
        positions (Tuple[List[int]): source positions
        signals (List[jnp.ndarray]): source signals

    !!! example
        ```python
        x_pos = [10,20,30,40]
        y_pos = [30,30,30,30]
        signal = jnp.sin(jnp.linspace(0,10,100))
        signals = jnp.stack([signal]*4)
        sources = geometry.Source(positions=(x_pos, y_pos), signals=signals)
        ```

    """
    positions: Tuple[jnp.ndarray]
    signals: Tuple[jnp.ndarray]


class ComplexSources(NamedTuple):
    r"""ComplexSources structure

    Attributes:
        positions (Tuple[List[int]): source positions
        amplitude (jnp.ndarray): source complex amplitudes

    !!! example
        ```python
        x_pos = [10,20,30,40]
        y_pos = [30,30,30,30]
        amp = jnp.array([0, 1, 1j, -1])
        sources = geometry.ComplexSources(positions=(x_pos, y_pos), amplitude=amp)
        ```
    """
    positions: Tuple[jnp.ndarray]
    amplitude: Tuple[jnp.ndarray]

    def to_field(self, grid):
        r"""Returns the complex field corresponding to the
        sources distribution.
        """
        field = jnp.zeros(grid.N, dtype=jnp.complex64)
        field = field.at[self.positions].set(self.amplitude)
        return field


class Sensors(NamedTuple):
    """Sensors structure

    Attributes:
        positions (Tuple[List[int]]): sensors positions

    !!! example
        ```python
        x_pos = [10,20,30,40]
        y_pos = [30,30,30,30]
        sensors = geometry.Sensors(positions=(x_pos, y_pos))
        ```

    """

    positions: Tuple[jnp.ndarray]


class TimeAxis(NamedTuple):
    r"""Temporal vector to be used for acoustic
    simulation based on the pseudospectral method of
    [k-Wave](http://www.k-wave.org/)

    Attributes:
        dt (float): time step
        t_end (float): simulation end time

    """
    dt: float
    t_end: float

    def to_array(self):
        r"""Returns the time-axis as an array"""
        return jnp.arange(0, self.t_end, self.dt)

    @staticmethod
    def from_kgrid(grid: kGrid, medium: Medium, cfl: float = 0.3, t_end=None):
        r"""Construct a `TimeAxis` object from `kGrid` and `Medium`

        Args:
            grid (kGrid):
            medium (Medium):
            cfl (float, optional):  The [CFL number](http://www.k-wave.org/). Defaults to 0.3.
            t_end ([float], optional):  The final simulation time. If None,
                    it is automatically calculated as the time required to travel
                    from one corner of the domain to the opposite one.

        """
        dt = dt = cfl * min(grid.dx) / jnp.max(medium.sound_speed)
        if t_end is None:
            t_end = jnp.sqrt(
                sum((x[-1] - x[0]) ** 2 for x in grid.space_axis)
            ) / jnp.min(medium.sound_speed)
        return TimeAxis(dt=dt, t_end=t_end)