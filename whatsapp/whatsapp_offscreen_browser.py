"""STUB no-op — WhatsApp removido."""
class _Stub:
    """No-op robusto: falsy, callable, sliceable, iterable, hashable, str vazio."""
    def __bool__(self): return False
    def __getattr__(self, _n):
        if _n.startswith("__"):  # nomes especiais (inspect/PySide): não é stub
            raise AttributeError(_n)
        return _Stub()
    def __call__(self, *a, **k): return _Stub()
    def __getitem__(self, _k): return _Stub()
    def __iter__(self): return iter(())
    def __len__(self): return 0
    def __str__(self): return ""
    def __contains__(self, _x): return False
    def get(self, *a, **k):
        return a[1] if len(a) > 1 else (k.get("default") if k else None)
    __hash__ = object.__hash__


def __getattr__(name):
    if name.startswith("__"):  # o PySide inspeciona módulos: __wrapped__ como stub dava "wrapper loop"
        raise AttributeError(name)
    return _Stub()
