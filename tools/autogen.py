"""
Parse public_api.h and generates various stubs around
"""
import attr
import re
import py
from copy import deepcopy
import pycparser
from pycparser import c_ast
from pycparser.c_generator import CGenerator

DISCLAIMER = """
/*
   DO NOT EDIT THIS FILE!

   This file is automatically generated by tools/autogen.py from tools/public_api.h.
   Run this to regenerate:
       make autogen

*/
"""

def toC(node):
    return toC.gen.visit(node)
toC.gen = CGenerator()


@attr.s
class Function:
    _CTX_NAME = re.compile(r'^_?HPy_?')

    name = attr.ib()
    node = attr.ib(repr=False)

    def _find_typedecl(self, node):
        while not isinstance(node, c_ast.TypeDecl):
            node = node.type
        return node

    def ctx_name(self):
        # e.g. "ctx_Module_Create"
        return self._CTX_NAME.sub(r'ctx_', self.name)

    def ctx_impl_name(self):
        return '&%s' % self.ctx_name()

    def is_varargs(self):
        return (len(self.node.type.args.params) > 0 and
                isinstance(self.node.type.args.params[-1], c_ast.EllipsisParam))

    def ctx_decl(self):
        # e.g. "HPy (*ctx_Module_Create)(HPyContext ctx, HPyModuleDef *def)"
        #
        # turn the function declaration into a function POINTER declaration
        newnode = deepcopy(self.node)
        newnode.type = c_ast.PtrDecl(type=newnode.type, quals=[])
        # fix the name of the function pointer
        typedecl = self._find_typedecl(newnode)
        # replace an ellipsis with a 'va_list _vl' argument
        if self.is_varargs():
            arg = c_ast.Decl('_vl', [], [], [],
                c_ast.TypeDecl('_vl', [],
                    c_ast.IdentifierType(['va_list'])),
                None, None)
            newnode.type.type.args.params[-1] = arg
        #
        typedecl.declname = self.ctx_name()
        return toC(newnode)

    def trampoline_def(self):
        # static inline HPy HPyModule_Create(HPyContext ctx, HPyModuleDef *def) {
        #      return ctx->ctx_Module_Create ( ctx, def );
        # }
        rettype = toC(self.node.type.type)
        parts = []
        w = parts.append
        w('static inline')
        w(toC(self.node))
        w('{\n    ')

        if not self.is_varargs():
            if rettype == 'void':
                w('ctx->%s' % self.ctx_name())
            else:
                w('return ctx->%s' % self.ctx_name())
            w('(')
            params = [p.name for p in self.node.type.args.params]
            w(', '.join(params))
            w(');')
        else:
            last_param = self.node.type.args.params[-2].name
            w('va_list _vl;')
            w('va_start(_vl, %s);' % last_param)
            if rettype == 'void':
                w('ctx->%s' % self.ctx_name())
            else:
                w('%s _res = ctx->%s' % (rettype, self.ctx_name()))
            w('(')
            params = [p.name for p in self.node.type.args.params[:-1]]
            params.append('_vl')
            w(', '.join(params))
            w(');')
            w('va_end(_vl);')
            if rettype != 'void':
                w('return _res;')

        w('\n}')
        return ' '.join(parts)

    def ctx_pypy_type(self):
        return 'rffi.VOIDP'


@attr.s
class GlobalVar:
    name = attr.ib()
    node = attr.ib(repr=False)

    def ctx_name(self):
        return self.name

    def ctx_impl_name(self):
        return '(HPy){CONSTANT_%s}' % (self.name.upper(),)

    def ctx_decl(self):
        return toC(self.node)

    def trampoline_def(self):
        return None

    def ctx_pypy_type(self):
        return 'HPy'


class FuncDeclVisitor(pycparser.c_ast.NodeVisitor):
    def __init__(self):
        self.declarations = []

    def visit_Decl(self, node):
        if isinstance(node.type, c_ast.FuncDecl):
            self._visit_function(node)
        elif isinstance(node.type, c_ast.TypeDecl):
            self._visit_global_var(node)

    def _visit_function(self, node):
        name = node.name
        if not name.startswith('HPy') and not name.startswith('_HPy'):
            print('WARNING: Ignoring non-hpy declaration: %s' % name)
            return
        for p in node.type.args.params:
            if hasattr(p, 'name') and p.name is None:
                raise ValueError("non-named argument in declaration of %s" %
                                 name)
        self.declarations.append(Function(name, node))

    def _visit_global_var(self, node):
        name = node.name
        if not name.startswith('h_'):
            print('WARNING: Ignoring non-hpy variable declaration: %s' % name)
            return
        assert toC(node.type.type) == "HPy"
        self.declarations.append(GlobalVar(name, node))


class AutoGen:

    def __init__(self, filename):
        self.ast = pycparser.parse_file(filename, use_cpp=True)
        #self.ast.show()
        self.collect_declarations()

    def collect_declarations(self):
        v = FuncDeclVisitor()
        v.visit(self.ast)
        self.declarations = v.declarations

    def gen_ctx_decl(self):
        # struct _HPyContext_s {
        #     int ctx_version;
        #     HPy h_None;
        #     ...
        #     HPy (*ctx_Module_Create)(HPyContext ctx, HPyModuleDef *def);
        #     ...
        # }
        lines = []
        w = lines.append
        w('struct _HPyContext_s {')
        w('    int ctx_version;')
        for f in self.declarations:
            w('    %s;' % f.ctx_decl())
        w('};')
        return '\n'.join(lines)

    def gen_ctx_def(self):
        # struct _HPyContext_s global_ctx = {
        #     .ctx_version = 1,
        #     .h_None = (HPy){CONSTANT_H_NONE},
        #     ...
        #     .ctx_Module_Create = &ctx_Module_Create,
        #     ...
        # }
        lines = []
        w = lines.append
        w('struct _HPyContext_s global_ctx = {')
        w('    .ctx_version = 1,')
        for f in self.declarations:
            name = f.ctx_name()
            impl = f.ctx_impl_name()
            w('    .%s = %s,' % (name, impl))
        w('};')
        return '\n'.join(lines)

    def gen_func_trampolines(self):
        lines = []
        for f in self.declarations:
            trampoline = f.trampoline_def()
            if trampoline:
                lines.append(trampoline)
                lines.append('')
        return '\n'.join(lines)

    def gen_pypy_decl(self):
        lines = []
        w = lines.append
        w("HPyContextS = rffi.CStruct('_HPyContext_s',")
        w("    ('ctx_version', rffi.INT_real),")
        for f in self.declarations:
            w("    ('%s', %s)," % (f.ctx_name(), f.ctx_pypy_type()))
        w("    hints={'eci': eci},")
        w(")")
        return '\n'.join(lines)


def main():
    root = py.path.local(__file__).dirpath().dirpath()
    universal_headers = root.join('hpy-api', 'hpy_devel', 'include', 'universal')
    autogen_ctx = universal_headers.join('autogen_ctx.h')
    autogen_func = universal_headers.join('autogen_func.h')
    autogen_ctx_def = root.join('cpython-universal', 'src', 'autogen_ctx_def.h')
    autogen_pypy = root.join('tools', 'autogen_pypy.txt')

    autogen = AutoGen(root.join('tools', 'public_api.h'))
    for func in autogen.declarations:
        print(func)

    ctx_decl = autogen.gen_ctx_decl()
    func_trampolines = autogen.gen_func_trampolines()
    ctx_def = autogen.gen_ctx_def()
    pypy_decl = autogen.gen_pypy_decl()

    with autogen_ctx.open('w') as f:
        print(DISCLAIMER, file=f)
        print(ctx_decl, file=f)

    with autogen_func.open('w') as f:
        print(DISCLAIMER, file=f)
        print(func_trampolines, file=f)

    with autogen_ctx_def.open('w') as f:
        print(DISCLAIMER, file=f)
        print(ctx_def, file=f)

    with autogen_pypy.open('w') as f:
        print(pypy_decl, file=f)

if __name__ == '__main__':
    main()
