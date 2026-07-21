"""AU Bulk Data Exchange (BDE) flat-file generators.

BDE is the ATO's fixed-length flat-file lodgment channel: files are
prepared locally and uploaded through Online Services for Business
(OS4B) file transfer. Unlike the SBR channel (``..sbr``) there is no
PVT — developers self-test against the file transfer test facility
(https://softwaredevelopers.ato.gov.au/portal-bde) and lodge production
files directly. Per the DPO ruling on DSPPT-49560 (2026-07-17) this is
the ONLY channel open to an in-house product for TPAR.
"""
from saebooks.jurisdictions.au.bde.tpar import (
    BdeAddress,
    BdePayee,
    BdePayer,
    BdeSender,
    TparBdeError,
    build_tpar_bde_file,
)

__all__ = [
    "BdeAddress",
    "BdePayee",
    "BdePayer",
    "BdeSender",
    "TparBdeError",
    "build_tpar_bde_file",
]
