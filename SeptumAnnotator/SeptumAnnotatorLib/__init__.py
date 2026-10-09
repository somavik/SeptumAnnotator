"""Core of the SeptumAnnotator tool.

Everything in this package is independent of 3D Slicer: it only needs numpy and
SimpleITK (scipy for the convex-hull region, openpyxl for .xlsx configs). The
Slicer module (SeptumAnnotator.py) is a thin GUI on top of it, so the same code
can be used for batch jobs and checked outside Slicer.

Array convention: numpy arrays are indexed (z, y, x), exactly as returned by
SimpleITK.GetArrayFromImage. Physical coordinates are SimpleITK/ITK LPS in mm.
"""

__version__ = "0.1.0"
PROTOCOL_VERSION = "0.2"
