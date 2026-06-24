# OpenEye: Cross-Device Eye Tracking for Head-Mounted Displays

**Gangtae Park, Mingyu Han, and Ian Oakley**  
*ETRA '26: Symposium on Eye Tracking Research and Applications*

[![DOI](https://img.shields.io/badge/DOI-10.1145%2F3806031-blue)](https://doi.org/10.1145/3806031)

---

## Overview

OpenEye is an open-source framework for **cross-device eye tracking on head-mounted displays (HMDs)** using the **Pupil Labs Neon** eye tracker.

The framework provides:

- Cross-device gaze tracking
- Calibration and mapping pipelines
- Signal filtering and logging
- Device-specific processing pipelines
- 3D-printable hardware mounts

Supported devices:

- Meta Quest 3
- Apple Vision Pro
- XREAL Air 2 Ultra

The project is designed to support reproducible eye-tracking research across heterogeneous XR platforms.

---

## Repository Structure

The repository is organized by device-specific directories.

```text
<Device>/
├── gui_unit/
│   ├── app/
│   │   └── app.py
│   │
│   └── core/
│       ├── config.py
│       ├── filter.py
│       ├── logger.py
│       ├── mapping.py
│       └── networking.py
│
├── processing_unit/
│
├── mount/
│   └── mount.stl
│
├── pyproject.toml
└── README.md
```

### Components

| Component | Description |
|---|---|
| GUI Unit | Calibration, gaze visualization, filtering, logging |
| Processing Unit | Device-specific rendering and interaction pipeline |
| Mount | 3D-printable hardware mount for Neon integration |

---

# Quick Start

## 1. Clone the Repository

```bash
git clone https://github.com/witlab-kaist/OpenEye.git
cd OpenEye
```

### Optional: Download Only One Device Directory

If you only need one device implementation:

```bash
git clone --filter=blob:none --no-checkout https://github.com/witlab-kaist/OpenEye.git
cd OpenEye

git sparse-checkout init --cone

# Choose one:
git sparse-checkout set quest
git sparse-checkout set avp
git sparse-checkout set xreal

git checkout
```

---

## 2. Install

```bash
pip install -e .
```

---

## 3. Run the GUI Application

```bash
# Choose one:
openeye-quest-gui
openeye-avp-gui
openeye-xreal-gui
```

---

## Configuration

Default device-specific parameters are defined in:

```text
<Device>/gui_unit/core/config.py
```

You can override them using a JSON configuration file:

```bash
openeye-<device>-gui --config path/to/config.json
```

### Configurable Parameters

- Sampling rate
- Filter cutoff frequency
- Mapping model parameters
- Evaluation task settings
- Canvas resolution

---

## Citation

```bibtex
@article{10.1145/3806031,
author = {Park, Gangtae and Han, Mingyu and Oakley, Ian},
title = {OpenEye: Cross-Device Eye Tracking for Head-Mounted Displays},
year = {2026},
issue_date = {May 2026},
publisher = {Association for Computing Machinery},
address = {New York, NY, USA},
volume = {10},
number = {3},
url = {https://doi.org/10.1145/3806031},
doi = {10.1145/3806031},
journal = {Proc. ACM Hum.-Comput. Interact.},
month = may,
articleno = {ETRA017},
numpages = {17},
keywords = {Eye Tracking, HMD, Evaluation, Toolkit}
}
```

## Acknowledgments

This work was supported by:

- National Research Foundation of Korea (NRF)
- IITP (Institute of Information & Communications Technology Planning & Evaluation)

as described in the accompanying paper.
