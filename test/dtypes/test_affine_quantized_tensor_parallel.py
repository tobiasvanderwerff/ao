import torch
import unittest
from torchao.testing.utils import copy_tests, TorchAOTensorParallelTestCase
from torch.testing._internal.common_utils import run_tests
from torch.testing._internal import common_utils
from torchao.quantization import int8_weight_only, float8_weight_only, float8_dynamic_activation_float8_weight
from torchao.quantization.observer import PerRow, PerTensor
import torch.distributed as dist
from torch.distributed._tensor import DTensor, Replicate, Shard, DeviceMesh
from torch.testing._internal.distributed._tensor.common_dtensor import (
    DTensorTestBase,
    with_comms,
    NUM_DEVICES,
)
from torchao.quantization.quant_api import quantize_
from torchao.dtypes import AffineQuantizedTensor
from torchao.utils import TORCH_VERSION_AT_LEAST_2_5

class TestInt8woAffineQuantizedTensorParallel(TorchAOTensorParallelTestCase):
    QUANT_METHOD_FN = staticmethod(int8_weight_only)
copy_tests(TorchAOTensorParallelTestCase, TestInt8woAffineQuantizedTensorParallel, "int8wo_tp")

# Run only on H100
if torch.cuda.is_available() and torch.cuda.get_device_capability() >= (9, 0):
    class TestFloat8woAffineQuantizedTensorParallel(TorchAOTensorParallelTestCase):
        QUANT_METHOD_FN = staticmethod(float8_weight_only)
    copy_tests(TorchAOTensorParallelTestCase, TestFloat8woAffineQuantizedTensorParallel, "fp8wo_tp")

# Run only on H100
if torch.cuda.is_available() and torch.cuda.get_device_capability() >= (9, 0):
    class TestFloat8dqAffineQuantizedTensorParallel(DTensorTestBase):
        """Basic test case for tensor subclasses
        """
        COMMON_DTYPES = [torch.bfloat16, torch.float16, torch.float32]
        TENSOR_SUBCLASS = AffineQuantizedTensor
        QUANT_METHOD_FN = staticmethod(float8_dynamic_activation_float8_weight)
        QUANT_METHOD_KWARGS = {}

        @staticmethod
        def colwise_shard(m: torch.nn.Module, mesh: DeviceMesh) -> torch.nn.Module:
            """
            Shard linear layer of the model in column-wise fashion
            """
            # Column-wise is wrt to A^T, so for A it is row-wise.
            # Number of rows per rank
            orig_weight = m.linear.weight
            n_local_rows = orig_weight.size(0) // mesh.size()
            rank = mesh.get_local_rank()
            local_shard = orig_weight[rank * n_local_rows : (rank + 1) * n_local_rows, :]
            # Construct DTensor from local shard
            dtensor = DTensor.from_local(local_shard, mesh, [Shard(0)])
            # Replace parameter in module
            m.linear.weight = torch.nn.Parameter(
                dtensor, requires_grad=False
            )
            return m

        @staticmethod
        def rowwise_shard(m: torch.nn.Module, mesh: DeviceMesh) -> torch.nn.Module:
            """
            Shard linear layer of the model in row-wise fashion
            """
            # Row-wise is wrt to A^T, so for A it is column-wise.
            # Number of rows per rank
            orig_weight = m.linear.weight
            n_local_cols = orig_weight.size(1) // mesh.size()
            rank = mesh.get_local_rank()
            local_shard = orig_weight[:, rank * n_local_cols : (rank + 1) * n_local_cols]
            # Construct DTensor from local shard
            dtensor = DTensor.from_local(local_shard, mesh, [Shard(1)], run_check=True)
            # Replace parameter in module
            m.linear.weight = torch.nn.Parameter(
                dtensor, requires_grad=False
            )
            return m

        def quantize(self, m: torch.nn.Module) -> torch.nn.Module:
            """
            Quantize the model
            """
            quantize_(m, self.QUANT_METHOD_FN(**self.QUANT_METHOD_KWARGS))
            return m

        def _test_tp(self, dtype):
            device = "cuda"
            # To make sure different ranks create the same module
            torch.manual_seed(5)

            class M(torch.nn.Module):
                def __init__(self, in_features, out_features, **kwargs) -> None:
                    super().__init__(**kwargs)
                    self.linear = torch.nn.Linear(in_features, out_features, bias=False, device="cuda")

                def forward(self, x: torch.Tensor) -> torch.Tensor:
                    return self.linear(x)

            # Get rank and device
            device = torch.device(f"cuda:{self.rank % torch.cuda.device_count()}")

            # Original model
            proj_up = M(1024, 2048).to(device).to(dtype)
            proj_dn = M(2048, 1024).to(device).to(dtype)
            example_input = 100 * torch.randn(128, 1024, device=device, dtype=dtype)
            y = proj_dn(proj_up(example_input))
            # Quantize the model
            up_quant = self.quantize(proj_up)
            dn_quant = self.quantize(proj_dn)
            y_q = dn_quant(up_quant(example_input))

            mesh = self.build_device_mesh()
            mesh.device_type = "cuda"

            # Shard the models
            up_dist = self.colwise_shard(up_quant, mesh)
            dn_dist = self.rowwise_shard(dn_quant, mesh)

            # We need to turn inputs into DTensor form as well -- just a format change
            input_dtensor = DTensor.from_local(
                example_input, mesh, [Replicate()]
            )

            y_d = dn_dist(up_dist(input_dtensor))

            if not TORCH_VERSION_AT_LEAST_2_5:
                # Need torch 2.5 to support compiled tensor parallelism
                return

            up_compiled = torch.compile(up_dist)
            y_up = up_compiled(input_dtensor)
            dn_compiled = torch.compile(dn_dist)
            y_dn = dn_compiled(y_up)

    class TestFloat8dqTensorAffineQuantizedTensorParallel(TestFloat8dqAffineQuantizedTensorParallel):
        QUANT_METHOD_FN = staticmethod(float8_dynamic_activation_float8_weight)
        QUANT_METHOD_KWARGS = {"granularity": PerTensor()}
        COMMON_DTYPES = [torch.bfloat16, torch.float16, torch.float32]

        @common_utils.parametrize("dtype", COMMON_DTYPES)
        @with_comms
        @unittest.skipIf(not torch.cuda.is_available(), "Need CUDA available")
        def test_tp(self, dtype):
            return self._test_tp(dtype)

    class TestFloat8dqRowAffineQuantizedTensorParallel(TestFloat8dqAffineQuantizedTensorParallel):
        QUANT_METHOD_FN = staticmethod(float8_dynamic_activation_float8_weight)
        QUANT_METHOD_KWARGS = {"granularity": PerRow()}
        COMMON_DTYPES = [torch.bfloat16]

        @common_utils.parametrize("dtype", COMMON_DTYPES)
        @with_comms
        @unittest.skipIf(not torch.cuda.is_available(), "Need CUDA available")
        def test_tp(self, dtype):
            return self._test_tp(dtype)
    
    common_utils.instantiate_parametrized_tests(TestFloat8dqTensorAffineQuantizedTensorParallel)
    common_utils.instantiate_parametrized_tests(TestFloat8dqRowAffineQuantizedTensorParallel)
if __name__ == "__main__":
    run_tests()