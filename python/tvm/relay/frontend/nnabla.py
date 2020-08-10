""" NNabla: Neural Network Libraries Frontend for Relay """
import numpy as np
import collections
import nnabla as nn
# from nnabla.utils.nnp_graph import NnpLoader, FunctionProto, VariableProto
from nnabla.utils import nnabla_pb2
from nnabla.utils.converter.nnabla import NnpImporter
# from nnabla.parameter import get_parameter_or_create, save_parameters, get_parameter_or_create
import google.protobuf.text_format as text_format 
import zipfile
import shutil
import tempfile
import os
import attr  
import sys 
import pdb

import tvm 
from tvm.ir import IRModule

from ... import nd as _nd
from .. import analysis 
from .. import expr as _expr
from .. import function as _function
from .. import op as _op
from ..expr_functor import ExprFunctor 

# from .common import AttrCvt, Renamer
from .common import get_relay_op, new_var, infer_shape, infer_channels
from .common import infer_type, get_name
from .common import infer_value as _infer_value
from .common import infer_value_simulated as _infer_value_simulated 

__all__ = ['from_nnabla']

# #############################################################################
# Helper functions
# ----------------
# 

# TENSOR_TYPE_TO_DTYPE = {
#     TensorProto.FLOAT: np.float32,
#     TensorProto.BOOL: np.bool,
#     TensorProto.UINT8: np.uint8,
#     TensorProto.INT8: np.int8,
#     TensorProto.INT32: np.uint32,
#     TensorProto.INT32: np.int32,
#     TensorProto.INT64: np.int64,
# }

def load_nnp(nnp_path):
    """ Add description """
    
    return NnpImporter(nnp_path, expad_network=False, executor_index=True).execute()

def default_layout(dims):
    """
    A helper function to get default layout 
    """
    if dims == 1:
        return 'NCW'
    elif dims == 2:
        return 'NCHW'
    elif dims == 3:
        return 'NCDHW'

    msg = "Only 1d, 2d and 3d layouts are currently supported"
    raise tvm.error.OpAttributeInvalid(msg.format(op_name))

def  dimension_picker(prefix, suffix=''):
    """ Check that dimensions are supported """
    # TODO: Check variables names
    def _impl(attr):
        kernel = attr['kernel_shape']
        if len(kernel) == 1:
            return prefix + '1d' + suffix 
        if len(kernel) == 2:
            return prefix + '2d' + suffix 
        if len(kernel) == 3:
            return prefix + '3d' + suffix 
        msg = 'Only 1D, 2D and 3D kernels are supported for operator {}.'
        op_name = prefix + '1d/2d/3d'
        raise tvm.error.OpAttributeInvalid(msg.format(op_name))

def dimension_constraint():
    """ A helper function to restric dimensions """
    def _dim_check(attrs):
        if len(attrs['kernel_shape']) in [1, 2, 3]:
            return True
        return False 
    
    return _dim_check, "Only 1d, 2d, 3d kernel supported."

def replace_negative_size_with_batch_size(shape, batch_size):
    """Replace all dimensions with negative values to batch size"""
    sl = []
    for d in shape.dim:
        if d < 0:
            # Negative size means batch size
            sl.append(batch_size)
        else:
            sl.append(d)
    out_shape = nnabla_pb2.Shape()
    out_shape.dim.extend(sl)
    return out_shape

# def get_tensor_type(name, type_dict):
#     if name in type_dict:
#         return type_dict[name]
#     else:
#         # Default tensor type to float
#         return TensorProto.FLOAT


# #############################################################################
# Operator definition
# -------------------
# 
# Each NNabla operator has its own converter 

def _none():
    def _impl(inputs, func):
        return None 
    return _impl 

def _convert_reshape():
    def _impl(inputs, func):
        if hasattr(func, 'shape'):
            return _op.reshape(inputs[0], func.shape)
        else:
            raise NotImplementedError("Yet to support dynamic input case")
            # return _op.reshape(inputs[0], inputs[1])
    return _impl

def _convert_concat():
    def _impl(inputs, func):
        return _op.concatenate(inputs, axis=func.axis)
    return _impl

def _convert_relu():
    def _impl(inputs, func):
        data = inputs[0]
        return _op.nn.relu(data)
    return _impl 

def _convert_convolution():
    def _impl(inputs, func, oarams):
        # TODO: Modify inputs -> _shape (dict with node name and dimension),
        # func -> func: Node function
        # shape -> params (parameters of the function)
        pdb.set_trace()
        # TODO: Map layouts. For that, include the shape dict from Exporter in order to get 
        # channel size and kernel size. data layout can be inferred with the shape
        # TODO: Check for all possible input combinations
        # for stride, pads, dilation, groups, channels, kernel_size 
        data_layout = "NCHW"
        kernel_layout = "OIHW"

        # Extract information from nnabla node found in convolution_param
        _stride = tuple(func.convolution_param.stride.dim)
        _pad_w = func.convolution_param.pad.dim[0]
        _pad_h = func.convolution_param.pad.dim[1]
        _pad  = (_pad_w, _pad_h, _pad_w, _pad_h)
        _dilation = tuple(func.convolution_param.dilation.dim)
        _group = func.convolution_param.group

        conv_out = _op.nn.conv2d(func.input[0],
                                 func.input[1],
                                 strides=_stride,
                                 padding=_pad,
                                 dilation=_dilation,
                                 groups=_group,
                                 channels= 1,#TODO obtain func.input[1].shape[0] from shape dict,
                                 kernel_size= 2, #TODO obtain func.input[1].shape[2:] from shape dict,
                                 data_layout=data_layout,
                                 kernel_layout=kernel_layout,
                                 out_layout="",
                                 out_dtype="")

        """ Alternative Way:
            out = AttrCvt(
                op_name=dimension_picker('conv'),
                transforms={
                    'kernel_shape': 'kernel_size',
                    'dilations': ('dilation', 1),
                    'pads': ('padding', 0),
                    'group': ('groups', 1)
                },
                custom_check=dimension_constraint())(inputs[:2], attr, params)
        """
        
        use_bias = len(inputs) == 3

        if use_bias:
            return _op.nn.bias_add(conv_out, inputs[func.input[2]])
        else:
            return conv_out 
    
    return _impl

def _linear():
    def _impl(inputs, func):
        # Equivalent Op to GEMM in ONNX
        # Y = alpha * A * B + beta * C(If exists)
        alpha = float(1.0)
        beta = float(1.0)

        # get number of channels 
        channels = infer_channels(inputs[1])
        inputs[0] = _op.nn.batch_flatten(inputs[0])
        out = _op.nn.dense(_expr.const(alpha) * inputs[0],
                           inputs[1], units=channels)
        
        use_bias = len(inputs) == 3

        if use_bias:
            return _op.nn.bias_add(out, _expr.const(beta) * inputs[2])
        else:
            return out 

    return _impl

# def _convert_softmax():


# def _convert_pooling():

# def _convert_batchnorm():

# def _convert_elemwise():

# def _convert_flatten():

# def _convert_affine():

# #############################################################################
# Converter map for NNabla 
# ------------------------
# 
# NNabla operators linked to the Relay converter
# 

_convert_map = {
    'SoftMax'                  : _none(), #_convert_softmax,
    'ReLU'                     : _none(), #_convert_relu,
    'LeakyReLU'                : _none(),
    'PReLU'                    : _none(),
    'ELU'                      : _none(),

    'AveragePooling'           : _none(), #_convert_pooling,
    'MaxPooling'               : _none(), #_convert_pooling,
    'GlobalAveragePooling2D'   : _none(),
    'GlobalMaxPooling2D'       : _none(),
    'Convolution'              : _convert_convolution(),
    'Conv2DTranspose'          : _none(),
    'DepthwiseConv2D'          : _none(),

    'Flatten'                  : _none(), #_convert_flatten,
    'Reshape'                  : _none(), #_convert_reshape,
    'Concatenate'              : _none(), #_convert_concat,
    'BatchNormalization'       : _none(), #_convert_batchnorm,
    'Add2'                     : _none(), #_convert_elemwise
    'Affine'                   : _none(),
}

# #############################################################################
# NNabla converter definiton
# --------------------------

def get_converter(op):
    """ Convert NNabla operators to Relay Converter """
    return _convert_map[op]

class NNablaGraph(object):
    def __init__(self, nnp, shape, dtype, batch_size=1):
        # NNabla related variables
        self._nnp = nnp.protobuf        # nnabla graph as protobuf object
        self._batch_size = batch_size   # executor batch_size
        self._net = None                # network_name
        self._executor = None
        self._parameters = {}
        self._var_dict = {}
        self.initializer = {}
        self.inputs = {}
        self.outputs = {}
        self.nodes = {}

    def _set_network(self):
        if len(self._nnp.executor) != 1:
            raise ValueError(
                "NNP with only a single executor is supported!")
        exe = self._nnp.executor[0]

        net = None 
        for n in self._nnp.network:
            if n.name == exe.network_name:
                net = n 
        if net is None:
            raise ValueError(
                "Executor network [{}] is not found in the NNP file.".format(exe.network_name))
        self._net = net 
        self._executor = exe 
        return net
    
    def _set_shape_all(self):
        assert isinstance(self._batch_size, int)
        bs = self._batch_size
        if bs < 0:
            bs = self._net._batch_size
        self._batch_size = bs
        # store all variable shape info to use later 
        for v in self._net.variable:
            self._var_dict[v.name] = replace_negative_size_with_batch_size(
                v.shape, bs)
        
        for p in self._nnp.parameter:
            self._parameters[p.variable_name] = p
    
    def _set_variables(self):
        exe = self._executor
        for param in self._nnp.parameter:
            if param.variable_name in self._var_dict:
                # Graph initializer
                self.initializer[param.variable_name] = param 

                # Graph Inputs
                self.inputs[param.variable_name] =  param 
        
            else:
                print("Not in: {}".format(param.variable_name))

        for iv in exe.data_variable:
            # Graph Inputs
            self.inputs[iv.variable_name] = iv
        for ov in exe.output_variable:
            # Only the final output of the graph is added
            self.outputs[ov.variable_name] = ov
        for gv in exe.generator_variable:
            # Graph Initializer
            self.initializer[gv.variable_name] = gv
            # Graph Inputs
            self.inputs[gv.variable_name] = gv
    
    def _set_nodes(self, func):
        """ Convert a function to a node or a group of nodes"""
        for f in self._net.function:
            node_name = f.name
            self.nodes[node_name] = f

    def create_graph(self):
        net = self._set_network()
        self._set_shape_all()
        for f in net.function:
            self._set_nodes(f)

        # Broadcast target buffer
        self._set_variables()

class Exporter(ExprFunctor):
    """ Add information """
    def __init__(self, nnp, shape, dtype, batch_size=1):
        # For creating Graph 
        self._nnp = nnp 
        self._batch_size = batch_size
        self._graph = NNablaGraph(nnp, shape, dtype, batch_size)

        # For Relay convertion
        self._nodes = {}
        self._params = {}
        self._num_input = 0
        self._num_param = 0
        self._shape = shape if shape else {}
        self._dtype = dtype

        # For infering Values
        self._temp_params = {}
        self._mod = None
        self._infer_simulated = True 
        super(Exporter, self).__init__()

    def _parse_array(self, param):
        """ Grab Nnabla parameter and return TVM NDArray """
        # TODO: Complete with dtype, for a start expect every type to be float32
        np_array = np.array(param.data, dtype="float32").reshape(tuple(param.shape.dim))
        
        return _nd.array(np_array)
    
    # def _parse_dtype(self, func):
    #     """ TODO: Create dtype parser to pass the correct datatype to Relay """
    
    def _convert_operator(self, input_data, func, params):
        """ Convert NNabla operator into Relay Operator
        The converter must specify conversions explicitly for incompatible name, and
        apply handlers to operator attributes.

        Parameters
        ----------
        input_data : dict of str
            Name of the inputs with its dimension shape

        func : nnabla_pb2.Function
            Function that describes a node. Contains the followinf information:
                -   name : Operator name
                -   type : Operator type
                -   inputs : input functions
                -   outputs : output functions
                -   param : Special attribute from each operator
        
        params : nnabla_pb2.Parameter
            Weights 

        Returns
        -------
        sym : tvm.relay.function.Function
            Converted relay function
        """
        op_name = func.type 
        if op_name in _convert_map:
            sym = _convert_map[op_name](input_data, func, params)
            pdb.set_trace()
        else:
            raise tvm.error.OpNotImplemented(
                'Operator {} is not supported for frontend NNabla.'.format(op_name))
        return sym

    def from_nnabla(self):
        """Construct Relay expression from NNabla graph.
        
        Nnabla graph is a protobuf object.
        
        Returns
        -------
        mod: tvm.IRModule
            The returned relay module
            
        params : dict
            A dict of name: tvm.nd.array pairs, used as pretrained weights
        """
        # Create NNabla graph
        # graph = NNablaGraph(self._nnp, self._shape, self._dtype, self._batch_size).create_graph()
        self._graph.create_graph()
        graph = self._graph
        
        # TODO: Convert dict of nnabla_pb2.shape into dict ot list/tuple
        self._shape = graph._var_dict

        # 1- parse network inputs or parameters to relay
        # 1.1 - Get parameters from graph initializer
        for init_param in graph.initializer:
            tmp_param = graph.initializer[init_param]

            assert init_param == tmp_param.variable_name
            self._params[tmp_param.variable_name] = self._parse_array(tmp_param)
            self._nodes[tmp_param.variable_name] = new_var(tmp_param.variable_name,
                                                            shape=self._params[init_param].shape,
                                                            dtype=self._params[init_param].dtype)

        # 1.2 - Get parameters from graph input
        for i in graph.inputs:
            i_name = graph.inputs[i].variable_name
            d_type = "float32" # Force datatype for now
            if i_name in self._params:
                # i is a param instead of an input
                self._num_param += 1
                self._params[i_name] = self._params.pop(i_name)
                self._nodes[i_name] = new_var(i_name,
                                              shape=self._params[i_name].shape,
                                              dtype=self._params[i_name].dtype)
            else:
                
                self._num_input += 1
                if i_name in self._shape:
                    tshape = list(self._shape[i_name].dim)
                else:
                    raise ValueError("Must provide an input shape for `{0}`.".format(i_name))
                if isinstance(self._dtype, dict):
                    dtype = self._dtype[i_name] if i_name in self._dtype else d_type
                else:
                    dtype= d_type
                assert isinstance(tshape, (list, tuple))
                self._nodes[i_name] = new_var(i_name, shape=tshape, dtype=dtype)
        # 2- get list of unsuppported ops
        unsupported_ops = set()
        for node in graph.nodes:
            op_name = graph.nodes[node].type 
            if op_name not in _convert_map and op_name != 'Constant':
                unsupported_ops.add(op_name)
        if unsupported_ops:
            msg = 'The following operators are not supported for frontend NNabla: '
            msg += ', '.join(unsupported_ops)
            raise tvm.error.OpNotImplemented(msg)

        # 3- construct nodes, nodes are stored as directed acyclic graph
        pdb.set_trace()
        for n in graph.nodes:
            op_name = graph.nodes[n].type
            node = graph.nodes[op_name]
            # inputs =  # Define input list or dict
            # Assert self._params type to be dictionary of str to tvm.nd.NDArray
            op = self._convert_operator(self._shape, node, self._params)
            node_output = node.output[0]
            if not isinstance(op, _expr.TupleWrapper):
                outputs_num = 1
            else:
                outputs_num = len(op)
            assert len(node.output) == outputs_num, (
                "Number of output mismatch {} vs {} in {}.".format(
                    len(node.output), outputs_num, op_name))
            if outputs_num == 1:
                self._nodes[node_output] = op
            else:
                for k, i in zip(list(node.output), range(len(node.output))):
                    self._nodes[k] = op[i]
            

        # 4- return the outputs
        pdb.set_trace()
        outputs = [self._nodes[i.variable_name] for i in graph.outputs]
        outputs = outputs[0] if len(outputs) == 1 else _expr.Tuple(outputs)
        func = _function.Function(analysis.free_vars(outputs), outputs)
        
        return IRModule.from_expr(func), self._params 

def from_nnabla(model, shape=None, dtype="float32"):
    """Convert NNabla model to relay Function.

    Parameters
    ----------
    model : NNabla.Nnp 
        The NNabla model to be converted in .nnp file, must contain
        the protobuf object

    shape: dict of str to int list/tuple
        Input shapes of the model, optional

    dtype : str or dict of str to str
        The input types to the graph

    Returns
    -------
    mod : tvm.IRModule
        The relay module for compilation.

    params : dict of str to tvm.nd.NDArray
        The parameter dict to be used by Relay.
    """
    try:
        import nnabla as nn
        from nnabla.utils.converter.nnabla import NnpImporter
        
        # TODO: Check model
    except ImportError:
        raise ImportError("Nnabla must be installed!")
    nnp = load_nnp(model)
    if nnp is not None:
        network_name = nnp.protobuf.executor[0].network_name
    else:
        print("Import from {} failed.".format(model))
    
    mod, params = Exporter(nnp, shape, dtype).from_nnabla()
    nnabla_model = None 
    
    return mod, params 






    




