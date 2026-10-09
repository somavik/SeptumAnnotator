cmake_minimum_required(VERSION 3.16.3...3.28 FATAL_ERROR)

project(SeptumAnnotator)

#-----------------------------------------------------------------------------
# Extension meta-information
# TODO: replace the two URLs once the repository is published.
set(EXTENSION_HOMEPAGE "https://github.com/OWNER/SeptumAnnotator")
set(EXTENSION_CONTRIBUTORS "Soma Szolár (BME MIT)")
set(EXTENSION_DESCRIPTION "Semi-automatic annotation of the interventricular septum in cardiac CT.")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/OWNER/SeptumAnnotator/main/SeptumAnnotator/Resources/Icons/SeptumAnnotator.png")
set(EXTENSION_SCREENSHOTURLS "")
set(EXTENSION_DEPENDS "NA")

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(SeptumAnnotator)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
