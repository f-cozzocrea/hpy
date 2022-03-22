"""
NOTE: these tests are also meant to be run as PyPy "applevel" tests.

This means that global imports will NOT be visible inside the test
functions. In particular, you have to "import pytest" inside the test in order
to be able to use e.g. pytest.raises (which on PyPy will be implemented by a
"fake pytest module")
"""
import pytest
from .support import HPyTest, DefaultExtensionTemplate

class CapsuleTemplate(DefaultExtensionTemplate):

    def DEFINE_strdup(self):
        return """
            #include <string.h>

            static char *strdup0(const char *s)
            {
                size_t n = strlen(s) + 1;
                char *copy = (char *) malloc(n * sizeof(char));
                if (copy == NULL) {
                    return NULL;
                }
                strncpy(copy, s, n);
                return copy;
            }
        """

    def DEFINE_SomeObject(self):
        return """
            #include <string.h>

            typedef struct {
                int value;
                char message[];
            } SomeObject;

            static SomeObject *create_payload(int value, char *message)
            {
                size_t n_message = strlen(message) + 1;
                SomeObject *pointer = (SomeObject *) 
                        malloc(sizeof(SomeObject) + n_message * sizeof(char));
                if (pointer == NULL) {
                    return NULL;
                }
                pointer->value = value;
                strncpy(pointer->message, message, n_message);
                return pointer;
            }
        """

    def DEFINE_Capsule_New(self, destructor="NULL"):
        return """
            #include <string.h>

            static const char *_capsule_name = "some_capsule";

            #define CAPSULE_NAME _capsule_name

            HPyDef_METH(Capsule_New, "capsule_new", capsule_new_impl, HPyFunc_VARARGS)
            static HPy capsule_new_impl(HPyContext *ctx, HPy self, HPy *args, HPy_ssize_t nargs)
            {
                int value;
                char *message;
                void *ptr;

                if (nargs > 0)
                {
                    if (!HPyArg_Parse(ctx, NULL, args, nargs, "is", &value, &message)) {
                        return HPy_NULL;
                    }
                    ptr = (void *) create_payload(value, message);
                    if (ptr == NULL) {
                        HPyErr_SetString(ctx, ctx->h_MemoryError, "out of memory");
                        return HPy_NULL;
                    }
                    return HPyCapsule_New(ctx, ptr, CAPSULE_NAME, (HPyCapsule_Destructor) %s);
                }
                /* just for error case testing */
                return HPyCapsule_New(ctx, NULL, CAPSULE_NAME, NULL);
            }
        """ % destructor

    def DEFINE_Payload_Free(self):
        return """
            #include <string.h>

            HPyDef_METH(Payload_Free, "payload_free", payload_free_impl, HPyFunc_O)
            static HPy payload_free_impl(HPyContext *ctx, HPy self, HPy arg)
            {
                const char *name = HPyCapsule_GetName(ctx, arg);
                if (name == NULL && HPyErr_Occurred(ctx)) {
                    return HPy_NULL;
                }

                void *pointer = HPyCapsule_GetPointer(ctx, arg, name);
                if (pointer == NULL && HPyErr_Occurred(ctx)) {
                    return HPy_NULL;
                }
                free(pointer);

                void *context = HPyCapsule_GetContext(ctx, arg);
                if (context == NULL && HPyErr_Occurred(ctx)) {
                    return HPy_NULL;
                }
                free(context);

                return HPy_Dup(ctx, ctx->h_None);
            }
        """

    def DEFINE_Capsule_GetName(self):
        return """
            HPyDef_METH(Capsule_GetName, "capsule_getname", capsule_getname_impl, HPyFunc_O)
            static HPy capsule_getname_impl(HPyContext *ctx, HPy self, HPy arg)
            {
                const char *name = HPyCapsule_GetName(ctx, arg);
                if (name == NULL) {
                    return HPy_NULL;
                }
                return HPyUnicode_FromString(ctx, name);
            }
        """

    def DEFINE_Capsule_GetPointer(self):
        return """
            static HPy payload_as_tuple(HPyContext *ctx, SomeObject *pointer)
            {
                HPy value = HPyLong_FromLong(ctx, pointer->value);
                HPy message = HPyUnicode_FromString(ctx, pointer->message);
                HPy result = HPyTuple_Pack(ctx, 2, value, message);
                HPy_Close(ctx, value);
                HPy_Close(ctx, message);
                return result;
            }

            HPyDef_METH(Capsule_GetPointer, "capsule_getpointer", capsule_get_payload_impl, HPyFunc_O)
            static HPy capsule_get_payload_impl(HPyContext *ctx, HPy self, HPy arg)
            {
                SomeObject *pointer = (SomeObject *) HPyCapsule_GetPointer(ctx, arg, CAPSULE_NAME);
                if (pointer == NULL) {
                    return HPy_NULL;
                }
                return payload_as_tuple(ctx, pointer);
            }
        """

class TestHPyCapsule(HPyTest):

    ExtensionTemplate = CapsuleTemplate

    def test_capsule_new(self):
        mod = self.make_module("""
            @DEFINE_SomeObject
            @DEFINE_Capsule_New
            @DEFINE_Capsule_GetName
            @DEFINE_Payload_Free

            @EXPORT(Capsule_New)
            @EXPORT(Capsule_GetName)
            @EXPORT(Payload_Free)

            @INIT
        """)
        p = mod.capsule_new(789, "Hello, World!")
        try:
            assert mod.capsule_getname(p) == "some_capsule"
        finally:
            # since HPy's capsule API does not allow a destructor, we need to
            # manually free the payload to avoid a memleak
            mod.payload_free(p)
        with pytest.raises(ValueError):
            mod.capsule_new()

    def test_capsule_getter_and_setter(self):
        mod = self.make_module("""
            #include <string.h>

            @DEFINE_strdup
            @DEFINE_SomeObject
            @DEFINE_Capsule_New
            @DEFINE_Capsule_GetPointer
            @DEFINE_Capsule_GetName
            @DEFINE_Payload_Free

            HPyDef_METH(Capsule_SetPointer, "capsule_setpointer", capsule_set_payload_impl, HPyFunc_VARARGS)
            static HPy capsule_set_payload_impl(HPyContext *ctx, HPy self, HPy *args, HPy_ssize_t nargs)
            {
                HPy capsule;
                int value;
                char *message;
                int non_null_pointer;
                if (!HPyArg_Parse(ctx, NULL, args, nargs, "Oisi", 
                                  &capsule, &value, &message, &non_null_pointer)) {
                    return HPy_NULL;
                }

                /* avoid memleak; get and later free previous pointer */
                void *old_ptr= HPyCapsule_GetPointer(ctx, capsule, CAPSULE_NAME);
                if (old_ptr == NULL && HPyErr_Occurred(ctx)) {
                    return HPy_NULL;
                }

                SomeObject *pointer = NULL;
                if (non_null_pointer) {
                    pointer = create_payload(value, message);
                    if (pointer == NULL) {
                        HPyErr_SetString(ctx, ctx->h_MemoryError, "out of memory");
                        return HPy_NULL;
                    }
                }

                if (HPyCapsule_SetPointer(ctx, capsule, (void *) pointer) < 0) {
                    if (non_null_pointer) {
                        free(pointer);
                    }
                    return HPy_NULL;
                }
                free(old_ptr);
                return HPy_Dup(ctx, ctx->h_None);
            }

            HPyDef_METH(Capsule_GetContext, "capsule_getcontext", capsule_get_context_impl, HPyFunc_O)
            static HPy capsule_get_context_impl(HPyContext *ctx, HPy self, HPy arg)
            {
                SomeObject *context = (SomeObject *) HPyCapsule_GetContext(ctx, arg);
                if (context == NULL) {
                    return HPyErr_Occurred(ctx) ? HPy_NULL : HPy_Dup(ctx, ctx->h_None);
                }
                return payload_as_tuple(ctx, context);
            }

            HPyDef_METH(Capsule_SetContext, "capsule_setcontext", capsule_set_context_impl, HPyFunc_VARARGS)
            static HPy capsule_set_context_impl(HPyContext *ctx, HPy self, HPy *args, HPy_ssize_t nargs)
            {
                HPy capsule;
                int value;
                char *message;
                if (!HPyArg_Parse(ctx, NULL, args, nargs, "Ois", &capsule, &value, &message)) {
                    return HPy_NULL;
                }

                /* avoid memleak; get and free previous context */
                void *old_context = HPyCapsule_GetContext(ctx, capsule);
                if (old_context == NULL && HPyErr_Occurred(ctx)) {
                    return HPy_NULL;
                }
                free(old_context);

                SomeObject *context = create_payload(value, message);
                if (context == NULL) {
                    HPyErr_SetString(ctx, ctx->h_MemoryError, "out of memory");
                    return HPy_NULL;
                }
                if (HPyCapsule_SetContext(ctx, capsule, (void *) context) < 0) {
                    return HPy_NULL;
                }
                return HPy_Dup(ctx, ctx->h_None);
            }

            HPyDef_METH(Capsule_SetName, "capsule_setname", capsule_set_name_impl, HPyFunc_VARARGS)
            static HPy capsule_set_name_impl(HPyContext *ctx, HPy self, HPy *args, HPy_ssize_t nargs)
            {
                HPy capsule;
                const char *name;
                if (!HPyArg_Parse(ctx, NULL, args, nargs, "Os", &capsule, &name)) {
                    return HPy_NULL;
                }

                /* avoid memleak; get and free previous context */
                const char *old_name = HPyCapsule_GetName(ctx, capsule);
                if (old_name == NULL && HPyErr_Occurred(ctx)) {
                    return HPy_NULL;
                }
                if (old_name != CAPSULE_NAME) {
                    free((void *) old_name);
                }

                char *name_copy = strdup0(name);
                if (name_copy == NULL) {
                    HPyErr_SetString(ctx, ctx->h_MemoryError, "out of memory");
                    return HPy_NULL;
                }

                if (HPyCapsule_SetName(ctx, capsule, (const char *) name_copy) < 0) {
                    return HPy_NULL;
                }
                return HPy_Dup(ctx, ctx->h_None);
            }

            HPyDef_METH(Capsule_free_name, "capsule_freename", capsule_free_name_impl, HPyFunc_O)
            static HPy capsule_free_name_impl(HPyContext *ctx, HPy self, HPy arg)
            {
                /* avoid memleak; get and free previous context */
                const char *old_name = HPyCapsule_GetName(ctx, arg);
                if (old_name == NULL && HPyErr_Occurred(ctx)) {
                    return HPy_NULL;
                }
                if (old_name != CAPSULE_NAME) {
                    free((void *) old_name);
                }
                return HPy_Dup(ctx, ctx->h_None);
            }

            @EXPORT(Capsule_New)
            @EXPORT(Capsule_GetPointer)
            @EXPORT(Capsule_SetPointer)
            @EXPORT(Capsule_GetContext)
            @EXPORT(Capsule_SetContext)
            @EXPORT(Capsule_GetName)
            @EXPORT(Capsule_SetName)
            @EXPORT(Capsule_free_name)
            @EXPORT(Payload_Free)

            @INIT
        """)
        p = mod.capsule_new(789, "Hello, World!")
        try:
            assert mod.capsule_getpointer(p) == (789, "Hello, World!")
            assert mod.capsule_setpointer(p, 456, "lorem ipsum", True) is None
            assert mod.capsule_getpointer(p) == (456, "lorem ipsum")

            assert mod.capsule_getcontext(p) == None
            assert mod.capsule_setcontext(p, 123, "hello") is None
            assert mod.capsule_getcontext(p) == (123, "hello")

            assert mod.capsule_getname(p) == "some_capsule"
            assert mod.capsule_setname(p, "foo") is None
            assert mod.capsule_getname(p) == "foo"

            not_a_capsule = "hello"
            with pytest.raises(ValueError):
                mod.capsule_getpointer(not_a_capsule)
            with pytest.raises(ValueError):
                mod.capsule_setpointer(not_a_capsule, 0, "", True)
            with pytest.raises(ValueError):
                mod.capsule_setpointer(p, 456, "lorem ipsum", False)
            #with pytest.raises(ValueError):
            #    mod.capsule_getcontext(not_a_capsule)
            #with pytest.raises(ValueError):
            #    mod.capsule_setcontext(not_a_capsule, 0, "")
            #with pytest.raises(ValueError):
            #    mod.capsule_getname(not_a_capsule)
            #with pytest.raises(ValueError):
            #    mod.capsule_setname(not_a_capsule, "")
        finally:
            # since HPy's capsule API does not allow a destructor, we need to
            # manually free the payload to avoid a memleak
            mod.payload_free(p)
            mod.capsule_freename(p)

    def test_capsule_isvalid(self):
        mod = self.make_module("""
            @DEFINE_SomeObject
            @DEFINE_Capsule_New
            @DEFINE_Capsule_GetName
            @DEFINE_Payload_Free

            HPyDef_METH(Capsule_isvalid, "capsule_isvalid", capsule_isvalid_impl, HPyFunc_VARARGS)
            static HPy capsule_isvalid_impl(HPyContext *ctx, HPy self, HPy *args, HPy_ssize_t nargs)
            {
                HPy capsule;
                const char *name;
                if (!HPyArg_Parse(ctx, NULL, args, nargs, "Os", &capsule, &name)) {
                    return HPy_NULL;
                }
                return HPyBool_FromLong(ctx, HPyCapsule_IsValid(ctx, capsule, name));
            }

            @EXPORT(Capsule_New)
            @EXPORT(Capsule_GetName)
            @EXPORT(Capsule_isvalid)
            @EXPORT(Payload_Free)

            @INIT
        """)
        p = mod.capsule_new(789, "Hello, World!")
        name = mod.capsule_getname(p)
        try:
            assert mod.capsule_isvalid(p, name)
            assert not mod.capsule_isvalid(p, "asdf")
            assert not mod.capsule_isvalid("asdf", name)
        finally:
            # manually free the payload to avoid a memleak since the
            # capsule doesn't have a destructor
            mod.payload_free(p)

    @pytest.mark.syncgc
    def test_capsule_new_with_destructor(self):
        mod = self.make_module("""
            static void my_destructor(const char *name, void *pointer, void *context);

            @DEFINE_SomeObject
            @DEFINE_Capsule_New(my_destructor)
            @DEFINE_Capsule_GetName
            @DEFINE_Payload_Free

            static int pointer_freed = 0;

            static void my_destructor(const char *name, void *pointer, void *context)
            {
                free(pointer);
                pointer_freed = 1;
            }

            HPyDef_METH(Pointer_freed, "pointer_freed", pointer_freed_impl, HPyFunc_NOARGS)
            static HPy pointer_freed_impl(HPyContext *ctx, HPy self)
            {
                return HPyBool_FromLong(ctx, pointer_freed);
            }

            @EXPORT(Capsule_New)
            @EXPORT(Capsule_GetName)
            @EXPORT(Pointer_freed)

            @INIT
        """)
        p = mod.capsule_new(789, "Hello, World!")
        assert mod.capsule_getname(p) == "some_capsule"
        del p
        assert mod.pointer_freed()

class TestHPyCapsuleLegacy(HPyTest):

    ExtensionTemplate = CapsuleTemplate

    def test_legacy_capsule_compat(self):
        import pytest
        mod = self.make_module("""
            @DEFINE_strdup

            #include <Python.h>
            #include <string.h>

            static int dummy = 123;

            static void legacy_destructor(PyObject *capsule)
            {
                /* We need to use C lib 'free' because the string was
                   created with 'strdup0'. */
                free((void *) PyCapsule_GetName(capsule));
            }

            HPyDef_METH(Create_pycapsule, "create_pycapsule", create_pycapsule_impl, HPyFunc_O)
            static HPy create_pycapsule_impl(HPyContext *ctx, HPy self, HPy arg)
            {
                HPy_ssize_t n;
                const char *name = HPyUnicode_AsUTF8AndSize(ctx, arg, &n);
                char *name_copy = strdup0(name);
                if (name_copy == NULL) {
                    HPyErr_SetString(ctx, ctx->h_MemoryError, "out of memory");
                    return HPy_NULL;
                }
                PyObject *legacy_caps = PyCapsule_New(&dummy, (const char *) name_copy, 
                                                      legacy_destructor);
                HPy res = HPy_FromPyObject(ctx, legacy_caps);
                Py_DECREF(legacy_caps);
                return res;
            }

            HPyDef_METH(Capsule_get, "get", get_impl, HPyFunc_O)
            static HPy get_impl(HPyContext *ctx, HPy self, HPy arg)
            {
                HPy res = HPy_NULL;
                HPy h_value = HPy_NULL;
                HPy has_destructor = HPy_NULL;
                HPyCapsule_Destructor destr = NULL;

                const char *name = HPyCapsule_GetName(ctx, arg);
                if (name == NULL && HPyErr_Occurred(ctx)) {
                    return HPy_NULL;
                }
                HPy h_name = HPyUnicode_FromString(ctx, name);
                if (HPy_IsNull(h_name)) {
                    goto finish;
                }

                int *ptr = (int *) HPyCapsule_GetPointer(ctx, arg, name);
                if (ptr == NULL && HPyErr_Occurred(ctx)) {
                    goto finish;
                }

                h_value = HPyLong_FromLong(ctx, *ptr);
                if (HPy_IsNull(h_value)) {
                    goto finish;
                }

                destr = HPyCapsule_GetDestructor(ctx, arg);
                if (destr == NULL && HPyErr_Occurred(ctx)) {
                    goto finish;
                }

                has_destructor = HPyBool_FromLong(ctx, destr != NULL);
                if (HPy_IsNull(has_destructor)) {
                    goto finish;
                }

                res = HPyTuple_Pack(ctx, 3, h_name, h_value, has_destructor);

            finish:
                HPy_Close(ctx, h_name);
                HPy_Close(ctx, h_value);
                HPy_Close(ctx, has_destructor);
                return res;
            }

            @EXPORT(Create_pycapsule)
            @EXPORT(Capsule_get)

            @INIT
        """)
        name = "legacy_capsule"
        p = mod.create_pycapsule(name)
        assert mod.get(p) == (name, 123, False)
