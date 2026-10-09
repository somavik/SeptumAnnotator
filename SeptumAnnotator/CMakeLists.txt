#-----------------------------------------------------------------------------
set(MODULE_NAME SeptumAnnotator)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  ${MODULE_NAME}Lib/__init__.py
  ${MODULE_NAME}Lib/config.py
  ${MODULE_NAME}Lib/imaging.py
  ${MODULE_NAME}Lib/manifest.py
  ${MODULE_NAME}Lib/openings.py
  ${MODULE_NAME}Lib/planes.py
  ${MODULE_NAME}Lib/seed.py
  ${MODULE_NAME}Lib/session.py
  )

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.png
  Resources/requirements.txt
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )
