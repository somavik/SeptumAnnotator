#-----------------------------------------------------------------------------
set(MODULE_NAME SeptumAnnotator)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  SeptumAnnotatorLib/__init__.py
  SeptumAnnotatorLib/case.py
  SeptumAnnotatorLib/geometry.py
  SeptumAnnotatorLib/labels.py
  SeptumAnnotatorLib/seed.py
  SeptumAnnotatorLib/skeleton.py
  SeptumAnnotatorLib/outflow.py
  SeptumAnnotatorLib/scene.py
  )

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.png
  Resources/UI/${MODULE_NAME}.ui
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )

#-----------------------------------------------------------------------------
if(BUILD_TESTING)

  # Register the unittest subclass in the main script as a ctest.
  # Note that the test will also be available at runtime.
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)

  # Additional build-time testing
  add_subdirectory(Testing)
endif()
