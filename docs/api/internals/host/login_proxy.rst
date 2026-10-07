login_proxy internals
=====================

The names ``otto.host.login_proxy`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/host/login_proxy`.

.. currentmodule:: otto.host.login_proxy

.. autoclass:: otto.host.login_proxy.LoginProxyFn

.. autoclass:: otto.host.login_proxy.LoginProxy

.. autoexception:: otto.host.login_proxy.LoginProxyError

.. autofunction:: otto.host.login_proxy.cred_identity

.. autofunction:: otto.host.login_proxy.cred_for

.. autoclass:: otto.host.login_proxy.HasVia

.. autofunction:: otto.host.login_proxy.via_cred

.. autofunction:: otto.host.login_proxy.default_login

.. autofunction:: otto.host.login_proxy.run_proxy

.. autofunction:: otto.host.login_proxy.run_undo

.. autofunction:: otto.host.login_proxy.perform_switch
