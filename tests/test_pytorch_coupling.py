import pytest

import torch

from firedrake import *
from firedrake_adjoint import *
from pyadjoint.tape import get_working_tape, pause_annotation

from physics_driven_ml.models import EncoderDecoder
from physics_driven_ml.utils import ModelConfig


pytorch_backend = load_backend("pytorch")


@pytest.fixture(autouse=True)
def handle_taping():
    yield
    tape = get_working_tape()
    tape.clear_tape()


@pytest.fixture(autouse=True, scope="module")
def handle_annotation():
    from firedrake_adjoint import annotate_tape, continue_annotation
    if not annotate_tape():
        continue_annotation()
    yield
    # Since importing firedrake_adjoint modifies a global variable, we need to
    # pause annotations at the end of the module
    annotate = annotate_tape()
    if annotate:
        pause_annotation()


@pytest.fixture(scope="module")
def mesh():
    return UnitSquareMesh(10, 10)


@pytest.fixture(scope="module")
def V(mesh):
    return FunctionSpace(mesh, "CG", 1)


@pytest.fixture
def f_exact(V, mesh):
    x, y = SpatialCoordinate(mesh)
    return Function(V).interpolate(sin(pi * x) * sin(pi * y))


# Set of Firedrake operations that will be composed with PyTorch operations
def poisson_residual(u, f, V):
    """Assemble the residual of a Poisson problem"""
    v = TestFunction(V)
    F = (inner(grad(u), grad(v)) + inner(u, v) - inner(f, v)) * dx
    return assemble(F)


# Set of Firedrake operations that will be composed with PyTorch operations
def solve_poisson(f, V):
    """Solve Poisson problem with homogeneous Dirichlet boundary conditions"""
    u = Function(V)
    v = TestFunction(V)
    F = (inner(grad(u), grad(v)) + inner(u, v) - inner(f, v)) * dx
    bcs = [DirichletBC(V, Constant(1.0), "on_boundary")]
    # Solve PDE
    solve(F == 0, u, bcs=bcs)
    # Assemble Firedrake loss
    return assemble(u ** 2 * dx)


@pytest.fixture(params=["poisson_residual", "solve_poisson"])
def firedrake_operator(request, f_exact, V):
    # Return firedrake operator and the corresponding non-control arguments
    if request.param == "poisson_residual":
        return poisson_residual, (f_exact, V)
    elif request.param == "solve_poisson":
        return solve_poisson, (V,)


@pytest.mark.skipcomplex  # Taping for complex-valued 0-forms not yet done
def test_pytorch_loss_backward(V, f_exact):
    """Test backpropagation through a vector-valued Firedrake operator"""

    # Instantiate model
    config = ModelConfig(input_shape=V.dim())
    model = EncoderDecoder(config)

    # Set double precision
    model.double()

    # Check that gradients are initially set to None
    assert all([θi.grad is None for θi in model.parameters()])

    # Convert f_exact to torch.Tensor
    f_P = pytorch_backend.to_ml_backend(f_exact)

    # Forward pass
    u_P = model(f_P)

    # Set control
    u = Function(V)
    c = Control(u)

    # Set reduced functional which expresses the Firedrake operations in terms of the control
    Jhat = ReducedFunctional(poisson_residual(u, f_exact, V), c)

    # Construct the torch operator that takes a callable representing the Firedrake operations
    G = torch_operator(Jhat)

    # Compute Poisson residual in Firedrake using the torch operator: `residual_P` is a torch.Tensor
    residual_P = G(u_P)

    # Compute PyTorch loss
    loss = (residual_P ** 2).sum()

    # -- Check backpropagation API -- #
    loss.backward()

    # Check that gradients were propagated to model parameters
    # This test doesn't check the correctness of these gradients
    # -> This is checked in `test_taylor_torch_operator`
    assert all([θi.grad is not None for θi in model.parameters()])

    # -- Check forward operator -- #
    u = pytorch_backend.from_ml_backend(u_P, V)
    residual = poisson_residual(u, f_exact, V)
    residual_P_exact = pytorch_backend.to_ml_backend(residual)

    assert (residual_P - residual_P_exact).detach().norm() < 1e-10


@pytest.mark.skipcomplex  # Taping for complex-valued 0-forms not yet done
def test_firedrake_loss_backward(V):
    """Test backpropagation through a scalar-valued Firedrake operator"""

    # Instantiate model
    config = ModelConfig(input_shape=V.dim())
    model = EncoderDecoder(config)

    # Set double precision
    model.double()

    # Check that gradients are initially set to None
    assert all([θi.grad is None for θi in model.parameters()])

    # Model input
    λ = Function(V)

    # Convert f to torch.Tensor
    λ_P = pytorch_backend.to_ml_backend(λ)

    # Forward pass
    f_P = model(λ_P)

    # Set control
    f = Function(V)
    c = Control(f)

    # Set reduced functional which expresses the Firedrake operations in terms of the control
    Jhat = ReducedFunctional(solve_poisson(f, V), c)

    # Construct the torch operator that takes a callable representing the Firedrake operations
    G = torch_operator(Jhat)

    # Solve Poisson problem and compute the loss defined as the L2-norm of the solution
    # -> `loss_P` is a torch.Tensor
    loss_P = G(f_P)

    # -- Check backpropagation API -- #
    loss_P.backward()

    # Check that gradients were propagated to model parameters
    # This test doesn't check the correctness of these gradients
    # -> This is checked in `test_taylor_torch_operator`
    assert all([θi.grad is not None for θi in model.parameters()])

    # -- Check forward operator -- #
    f = pytorch_backend.from_ml_backend(f_P, V)
    loss = solve_poisson(f, V)
    loss_P_exact = pytorch_backend.to_ml_backend(loss)

    assert (loss_P - loss_P_exact).detach().norm() < 1e-10


@pytest.mark.skipcomplex  # Taping for complex-valued 0-forms not yet done
def test_taylor_torch_operator(firedrake_operator, V):
    """Taylor test for the torch operator"""
    # Control value
    ω = Function(V)
    # Get Firedrake operator and other operator arguments
    fd_op, args = firedrake_operator
    # Set reduced functional
    Jhat = ReducedFunctional(fd_op(ω, *args), Control(ω))
    # Define the torch operator
    G = torch_operator(Jhat)
    # `gradcheck` is likely to fail if the inputs are not double precision (cf. https://pytorch.org/docs/stable/generated/torch.autograd.gradcheck.html)
    x_P = torch.rand(V.dim(), dtype=torch.double, requires_grad=True)
    # Taylor test (`eps` is the perturbation)
    assert torch.autograd.gradcheck(G, x_P, eps=1e-6)
