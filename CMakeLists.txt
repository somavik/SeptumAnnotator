cmake_minimum_required(VERSION 3.20.6...3.22.6 FATAL_ERROR)

project(SeptumAnnotatorExtension)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://www.slicer.org/wiki/Documentation/Nightly/Extensions/SeptumAnnotatorExtension")
set(EXTENSION_CONTRIBUTORS "Szolár Soma (BME MIT)")
set(EXTENSION_DESCRIPTION "3D Slicer extension for semi-automatic annotation of the interventricular septum on cardiac CT")
set(EXTENSION_ICONURL "https://www.example.com/Slicer/Extensions/SeptumAnnotatorExtension.png")
set(EXTENSION_SCREENSHOTURLS "https://www.example.com/Slicer/Extensions/SeptumAnnotatorExtension/Screenshots/1.png")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

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
