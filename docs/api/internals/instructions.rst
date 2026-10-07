instructions internals
======================

The names ``otto.instructions`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/instructions`.

.. currentmodule:: otto.instructions

.. autoclass:: otto.instructions.InstructionEntry

.. autodata:: otto.instructions.INSTRUCTIONS

.. autodata:: otto.instructions.FIRST_PARTY_INSTRUCTIONS

.. autodata:: otto.instructions.MARK_ATTR

.. autodata:: otto.instructions.PREVIEWABLE_INSTRUCTIONS

.. autoexception:: otto.instructions.ProjectInstructionError

.. autoclass:: otto.instructions.ProjectInstructionMark

.. autoclass:: otto.instructions.ProjectInstructionSpec

.. autoclass:: otto.instructions.ProjectInstructionBody

.. autoclass:: otto.instructions.ProjectInstruction

.. autofunction:: otto.instructions.command_name

.. autofunction:: otto.instructions.options_parameter

.. autodata:: otto.instructions.PROJECT_INSTRUCTIONS

.. autofunction:: otto.instructions.register_project_instruction_body

.. autodata:: otto.instructions.P

.. autofunction:: otto.instructions.bind_handler_kwargs
