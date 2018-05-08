from argparse import ArgumentParser
import ast
from collections import defaultdict
import pickle
import textwrap

from astunparse import unparse
import networkx as nx
import pandas as pd
from plpy.analyze.dynamic_trace_events import ExecLine

from .identify_donations import ColumnUse, ColumnDef, remove_duplicate_graphs


### Parameters and Return values ###
def comment_out_nodes(graph, predicate):
    # predicate can check things like if it reads from disk
    for _, node_data in graph.nodes(data=True):
        node_data['treat_as_comment'] = predicate(node_data)
    return graph

def is_pandas_read(node_data):
    calls = node_data['calls']
    if calls is None:
        return False
    for call_event in calls:
        if call_event.details['qualname'] == pd.read_csv.__qualname__:
            return True
    return False

def annotate_dataframe_uses(graph):
    # defined as a load of a reference with a dataframe type
    for _, node_data in graph.nodes(data=True):
        node_data['dataframe_uses'] = set([])
        if node_data.get('treat_as_comment', False):
            continue
        if isinstance(node_data['event'], ExecLine):
            uses = node_data['uses']
            df_uses = [u for u in uses if u.type == pd.DataFrame.__name__]
            node_data['dataframe_uses'].update(df_uses)
    return graph

# TODO: consider that we might want to annotate
# entire graph with the first/last LHS uses of these references
def annotate_dataframe_defs(graph):
    # defined as an assignment (LHS) that involves a reference to a dataframe type
    for _, node_data in graph.nodes(data=True):
        node_data['dataframe_defs'] = set([])
        if node_data.get('treat_as_comment', False):
            continue
        if isinstance(node_data['event'], ExecLine):
            defs = node_data['complete_defs']
            df_defs = [d for d in defs if d.type == pd.DataFrame.__name__]
            node_data['dataframe_defs'].update(df_defs)
    return graph

def annotate_dataframe_mods(graph):
    # defined as an assignment (LHS) that involves a reference to a dataframe type
    # and the same reference is previously defined
    already_defined = set([])
    for _, node_data in graph.nodes(data=True):
        node_data['dataframe_mods'] = set([])
        if node_data.get('treat_as_comment', False):
            continue
        if isinstance(node_data['event'], ExecLine):
            dataframe_defs = node_data['dataframe_defs']
            mods = set([df for df in dataframe_defs if df in already_defined])
            node_data['dataframe_mods'].update(mods)
            already_defined.update(dataframe_defs)
    return graph

def node_data_in_exec_order(graph):
    return sorted(graph.nodes(data=True), key=lambda x: x[1]['event'].event_id)

class GetNames(ast.NodeVisitor):
    def __init__(self):
        self.acc = set([])

    def visit_Name(self, node):
        self.acc.add(node.id)

    def run(self, node):
        self.visit(node)
        return self.acc

def can_be_computed(var, defined):
    var_tree = ast.parse(var.name)
    uses_names = GetNames().run(var_tree)
    defined_names = set(d.name for d in defined)
    return uses_names.issubset(defined_names)

def get_free_input_dataframes(graph):
    global_free = set([])
    defined = set([])
    sorted_by_exec = node_data_in_exec_order(graph)
    for _, node_data in sorted_by_exec:
        if node_data.get('treat_as_comment', False):
            continue
        for var in node_data['dataframe_uses']:
            if not var in defined and not can_be_computed(var, defined):
                global_free.add(var)
        # update with all definitions, not just dataframes
        # FIXME: shoudl this be 'defs' or 'complete_defs'?
        defined.update(node_data['complete_defs'])
    return global_free

# TODO: should this actually be based exclusively on memory location?
# currently its name/mem-location
def get_returnable_dataframes(graph):
    returnable = set([])
    sorted_by_exec = node_data_in_exec_order(graph)
    for _, node_data in sorted_by_exec:
        if node_data.get('treat_as_comment', False):
            continue
        # we care to return only dataframes that have
        # been defined or modified somehow
        returnable.update(node_data['dataframe_defs'])
        returnable.update(node_data['dataframe_mods'])
    return returnable

def trim_suffix_trace_based_on_return(graph, return_ref):
    # remove any statements that occur after the last def/modification
    # of a given dataframe ref
    sorted_by_exec = node_data_in_exec_order(graph)
    # find last use
    last_lhs_event_id = None
    for node_id, node_data in sorted_by_exec:
        if return_ref in node_data['dataframe_defs'] or return_ref in node_data['dataframe_mods']:
            last_lhs_event_id = node_data['event'].event_id
    if last_lhs_event_id is None:
        raise Exception('{} never used in graph'.format(return_ref))

    trimmed_node_ids = []
    for node_id, node_data in graph.nodes(data=True):
        if node_data['event'].event_id <= last_lhs_event_id:
            trimmed_node_ids.append(node_id)

    trimmed = graph.subgraph(trimmed_node_ids)
    return trimmed.to_directed()


### Adding user code that we may not get to observe through trace ###
# collects functions and user class definitions
# doesn't account for things like named lambdas
class CollectUserDefinedCallableNames(ast.NodeVisitor):
    def __init__(self):
        self.names = []

    def run(self, tree):
        self.visit(tree)
        return self.names

    def visit_ClassDef(self, node):
        self.names.append(node.name)

    def visit_FunctionDef(self, node):
        # we ignore things that may be private by convention
        if not node.name.startswith('_'):
            self.names.append(node.name)

# collect body (and dependencies) for user callables
class CollectUserDefinedCallableBodies(ast.NodeVisitor):
    def __init__(self, names):
        self.names = names
        self.name_to_body = {}
        self.name_to_dependencies = defaultdict(lambda: set([]))
        self.inside_callable = []
        self.name_to_complete_code = defaultdict(lambda: [])

    def visit_Name(self, node):
        if self.inside_callable and node.id in self.names:
            enclosing_name = self.inside_callable[-1]
            self.name_to_dependencies[enclosing_name].append(node.id)

    def visit_FunctionDef(self, node):
        self.name_to_body[node.name] = unparse(node)
        self.inside_callable.append(node.name)
        self.generic_visit(node)
        self.inside_callable.pop()

    def visit_ClassDef(self, node):
        self.name_to_body[node.name] = unparse(node)
        self.inside_callable.append(node.name)
        self.generic_visit(node)
        self.inside_callable.pop()

    def _populate_user_def_code0(self, name):
        # this is recursive for now, for clarity, but Python
        # can hit recursion error pretty easily. shouldn't be an issue
        # here since the definitions are likely not to be deeply nested
        if name in self.name_to_complete_code:
            return self.name_to_complete_code[extra]
        else:
            code = []
            code.append(self.name_to_body[name])

            depends_on = self.name_to_dependencies[name]
            for dep in depends_on:
                code.extend(self._populate_user_def_code0(dep))

            self.name_to_complete_code[name] = code
            return code

    def _populate_user_def_code(self):
        for name in self.names:
            self._populate_user_def_code0(name)

    def run(self, tree):
        self.visit(tree)
        self._populate_user_def_code()

    def get_code(self, names):
        if isinstance(names, str):
            names = [names]
        # portion of names that (may) be user-defined functions and should be added
        user_names = set(names).intersection(self.names)

        code = set([])
        for name in user_names:
            code.update(self.name_to_complete_code[name])
        return list(code)


def build_user_code_map(tree):
    names = CollectUserDefinedCallableNames().run(tree)
    code_map = CollectUserDefinedCallableBodies(names)
    code_map.run(tree)
    return code_map

def get_user_defined_callables(graph, user_code_map):
    names = set([])
    for _, node_data in graph.nodes(data=True):
        # type == 'type' should pick up user defined classes
        names.update([u.name for u in node_data['uses'] if u.type == 'function' or u.type == 'type'])
    return user_code_map.get_code(names)

def graph_to_lines(graph):
    sorted_nodes = node_data_in_exec_order(graph)
    code = []
    for _, node_data in sorted_nodes:
        line = '{comment_marker}{src}'.format(
            comment_marker='# ' if node_data.get('treat_as_comment', False) else '',
            src=node_data['src']
        )
        code.append(line)
    return code

### the donated function class ###
class DonatedFunction(object):
    def __init__(self, graph, name, formal_arg_vars, return_var, cleaning_code, context_code=None):
        # make sure to have a copy of the graph backing this function
        # graph has some meta-data we care about as well
        self.graph = graph.to_directed()

        # function definition
        self.name = name
        # sort by name just for deterministic order
        self.formal_arg_vars = sorted(list(formal_arg_vars), key=lambda x: x.name)
        self.return_var = return_var
        # source code
        self.cleaning_code = cleaning_code
        self.context_code = context_code
        self._source = None
        self._ast = None

        # executable function
        self._obj = None

    @property
    def ast(self):
        if self._ast is None:
            tree = ast.parse(self.source)
            self._ast = tree
        return self._ast

    @property
    def source(self):
        if self._source is None:
            template = 'def {name}({formal_args_str}):\n{context_code_str}{core_code_str}'

            # arguments
            formal_args_str = ','.join([a.name for a in self.formal_arg_vars])

            # context code
            if self.context_code:
                context_code_lines = ['# additional context code from user definitions']
                for _def in self.context_code:
                    # remove any body-specific indentation prior to adding in
                    _def = textwrap.dedent(_def)
                    if not _def.startswith('\n'):
                        _def = '\n' + _def
                    context_code_lines.append(_def)
                # everything will be indented by a tab to be properly defined
                # inside the broader function
                context_code_lines.append('\n')
                context_code_str = ''.join(context_code_lines)
                context_code_str = textwrap.indent(context_code_str, '\t')
            else:
                context_code_str = ''

            # core cleaning code
            core_code_lines = ['# core cleaning code']
            core_code_lines.extend(self.cleaning_code)
            if self.return_var:
                core_code_lines.append('return {}'.format(self.return_var.name))
            core_code_str = '\n'.join(core_code_lines)
            core_code_str = textwrap.indent(core_code_str, '\t')

            code = template.format(
                name=self.name,
                formal_args_str=formal_args_str,
                context_code_str=context_code_str,
                core_code_str=core_code_str
            )
            self._source = code

        return self._source

    @property
    def obj(self):
        if self._obj is None:
            _globals = {}
            exec(self.source, _globals)
            self._obj = _globals[self.name]
        return self._obj

    def __call__(self, *args):
        return self.obj(*args)


def lift_to_functions(graphs, script_src, name_format=None, name_counter=None):
    if isinstance(graphs, nx.DiGraph):
        graphs = [graphs]

    if name_format is None:
        name_format = 'cleaning_function_%d'

    if name_counter is None:
        name_counter = 0

    # we need to add user code for functions that may get executed but we don't directly trace
    tree = ast.parse(script_src)
    user_code_map = build_user_code_map(tree)

    function_data = {}
    trimmed_traces = []
    # TODO: fix this horrible hack of id to remove duplicates later
    _id = 0
    for graph in graphs:
        # create copy before we annotate/modify
        graph = graph.to_directed()

        # Graph annotation
        # we remove dataframe reads and create free (dataframe) variables as a result
        graph = comment_out_nodes(graph, is_pandas_read)
        # annotate with uses/defs (and modifications) for dataframes
        graph = annotate_dataframe_uses(graph)
        graph = annotate_dataframe_defs(graph)
        graph = annotate_dataframe_mods(graph)

        # free dataframe variables become arguments for the function
        formal_args = get_free_input_dataframes(graph)
        # possible returns are dataframes created or modified
        possible_returns = get_returnable_dataframes(graph)

        # we can created many functions based on the return value by trimming suffixes
        for _return in possible_returns:
            trimmed = trim_suffix_trace_based_on_return(graph, _return)
            trimmed.graph['_id'] = _id
            function_data[_id] = (graph, trimmed, formal_args, _return)
            trimmed_traces.append(trimmed)
            _id += 1

    # Remove traces that are duplciates now that we have removed suffixes
    # TODO: fix this other part of the horrible hack
    print('Identified {} functions'.format(len(trimmed_traces)))
    unique_trimmed = remove_duplicate_graphs(trimmed_traces)
    unique_function_data = []
    for trimmed in unique_trimmed:
        unique_function_data.append(function_data[trimmed.graph['_id']])
    print('Lifting {} functions after removing duplicates'.format(len(unique_function_data)))

    functions = []
    for graph, trimmed_trace, formal_args, _return in unique_function_data:
        # add in additional user code needed for execution (context), such as user function/class defs
        context_code = get_user_defined_callables(trimmed_trace, user_code_map)
        # convert graph to lines of code
        cleaning_code = graph_to_lines(trimmed_trace)
        function_name = name_format % name_counter
        func = DonatedFunction(trimmed_trace, function_name, formal_args, _return, cleaning_code, context_code)
        functions.append(func)
        name_counter += 1

    return functions


def main(args):
    with open(args.graph_file, 'rb') as f:
        graphs = pickle.load(f)

    with open(args.src_file, 'r') as f:
        script_src = f.read()

    functions = lift_to_functions(graphs, script_src, name_format=args.name_format, name_counter=args.name_counter)
    print('Collected {} functions'.format(len(functions)))

    with open(args.output_file, 'wb') as f:
        pickle.dump(functions, f)

if __name__ == '__main__':
    parser = ArgumentParser(description='Lift collection of donation graphs to Python functions')
    parser.add_argument('graph_file', type=str, help='Path to pickled donation graphs')
    parser.add_argument('src_file', type=str, help='Path to original user script to collect additional defs')
    parser.add_argument('output_file', type=str, help='Path to store pickled functions')
    parser.add_argument('-n', '--name_format', type=str, help='String formatting for function name', default='cleaning_func_%d')
    parser.add_argument('-c', '--name_counter', type=int, help='Initialize function counter for naming', default=0)
    args = parser.parse_args()
    try:
        main(args)
    except Exception as err:
        import pdb
        pdb.post_mortem()


# TODO:
# 1 - Try this with other scripts
# 3 - Try to lift without input args (i.e. don't comment reads out): => basic function executable check
# 4 - Try lifting more
# 5 - Start tests for this project: (convert candidates, identify donations, and lift) we'll live without tests for collect (since browser based)


# Notes on things we need to do
# 1 - Repair slices by adding missing variable bindings
#     * uses x @ mem, but missing, but have y @ mem' s.t mem = mem', add x = y statement
# 3 - Remove prefix based on return value (not sure if we actually want to do this right now...)
#     * identify where that memory is initialized and remove everything before it
#     * can turn init statement into free variable by commenting out (this generalizes the read_csv case)
