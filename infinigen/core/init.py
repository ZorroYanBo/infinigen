# Copyright (c) Princeton University.
# This source code is licensed under the BSD 3-Clause license found in the LICENSE file in the root directory of this source tree.
import bpy

import argparse
import ast
import os
import random
import sys
import cProfile
import shutil
from pathlib import Path
import logging
from functools import partial
import pprint
from collections import defaultdict

# ruff: noqa: F402
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"  # This must be done BEFORE import cv2. 
# See https://github.com/opencv/opencv/issues/21326#issuecomment-1008517425

import gin
import numpy as np
from numpy.random import randint

from infinigen.core.util.math import int_hash
from infinigen.core.util.organization import Task
from infinigen.core.util.logging import Suppress, LogLevel

logger = logging.getLogger(__name__)

CYCLES_GPUTYPES_PREFERENCE = [

    # key must be a valid cycles device_type
    # ordering indicate preference - earlier device types will be used over later if both are available
    #  - e.g most OPTIX gpus will also show up as a CUDA gpu, but we will prefer to use OPTIX due to this list's ordering

    'OPTIX',
    'CUDA',  
    'METAL', # untested
    'HIP', # untested
    'ONEAPI', # untested
    'CPU',
]

def parse_args_blender(parser):
    if '--' in sys.argv:
        # Running using a blender commandline python. 
        # args before '--' are intended for blender not infinigen
        argvs = sys.argv[sys.argv.index('--')+1:]
        return parser.parse_args(argvs)
    else:
        return parser.parse_args()
    

def parse_seed(seed, task=None):

    if seed is None:
        if task is not None and Task.Coarse not in task:
            raise ValueError(
                'Running tasks on an already generated scene, you need to specify --seed or results will'
                ' not be view-consistent')
        return randint(1e7), 'chosen at random'

    # WARNING: Do not add support for decimal numbers here, it will cause ambiguity, as some hex numbers are valid decimals

    try:
        return int(seed, 16), 'parsed as hexadecimal'
    except ValueError:
        pass

    return int_hash(seed), 'hashed string to integer'

def apply_scene_seed(seed, task=None):
    scene_seed, reason = parse_seed(seed, task)
    logger.info(f'Converted {seed=} to {scene_seed=}, {reason}')
    gin.constant('OVERALL_SEED', scene_seed)
    random.seed(scene_seed)
    np.random.seed(scene_seed)
    return scene_seed

def sanitize_override(override: list):

    if (
        ('=' in override) and 
        not any((c in override) for c in "\"'[]")
    ):
        k, v = override.split('=')
        try:
            ast.literal_eval(v)
        except (ValueError, SyntaxError):
            if "@" not in v:
                override = f'{k}="{v}"'

    return override

def repo_root():
    return Path(__file__).parent.parent.parent

def contained_stems(filenames: list[str], folder: Path):
    assert folder.exists()
    names = [p.stem for p in folder.iterdir()]
    return {s.stem in names or s.name in names for s in map(Path, filenames)}

def resolve_folder_maybe_relative(folder, root):
    folder = Path(folder)
    if folder.exists():
        return folder
    folder_rel = root/folder
    if folder_rel.exists():
        return folder_rel
    raise FileNotFoundError(f'Could not find {folder} or {folder_rel}')

@gin.configurable
def apply_gin_configs(
    configs_folder: Path,
    configs: list[str] = None,
    overrides: list[str] = None, 
    skip_unknown: bool = False, 
    mandatory_folders: list[Path] = None,
    mutually_exclusive_folders: list[Path] = None
):
    
    """
    Apply gin configuration files and bindings.

    Parameters
    ----------
    configs_folder : Path
        The path to the toplevel folder containing the gin configuration files.
    configs : list[str]
        A list of filenames to find within the configs_folder.
    overrides : list[str]
        A list of gin-formatted pairs to override the configs with.
    skip_unknown : bool
        If True, ignore errors for configs that were set by the user but not used anywhere
    mandatory_folders : list[Path]
        For each folder in the list, at least one config file must be loaded from that folder.
    mutually_exclusive_folders : list[Path]
        For each folder in the list, at most one config file must be loaded from that folder.
        
    """

    if configs is None:
        configs = []
    if overrides is None:
        overrides = []
    if mandatory_folders is None:
        mandatory_folders = []
    if mutually_exclusive_folders is None:
        mutually_exclusive_folders = []
    configs_folder = Path(configs_folder)

    root = repo_root()

    configs_folder_rel = root/configs_folder
    if configs_folder_rel.exists():
        configs_folder = configs_folder_rel
        gin.add_config_file_search_path(configs_folder)
    elif configs_folder.exists():
        gin.add_config_file_search_path(configs_folder)
    else:
        raise FileNotFoundError(f'Couldnt find {configs_folder} or {configs_folder_rel}')
        
    search_paths = [configs_folder, root, Path('.')]

    def find_config(p):
        p = Path(p)
        for folder in search_paths:
            for file in folder.glob('**/*.gin'):
                if file.stem == p.stem:
                    return file
        raise FileNotFoundError(f'Could not find {p} or {p.stem} in any of {search_paths}')
      
    configs = [find_config(g) for g in ['base.gin'] + configs]
    overrides = [sanitize_override(o) for o in overrides]

    for mandatory_folder in mandatory_folders:
        mandatory_folder = resolve_folder_maybe_relative(mandatory_folder, root)
        if not contained_stems(configs, mandatory_folder):
            raise FileNotFoundError(
                f'At least one config file must be loaded from {mandatory_folder} to avoid unexpected behavior'
            )
        
    for mutex_folder in mutually_exclusive_folders:
        mutex_folder = resolve_folder_maybe_relative(mutex_folder, root)
        stems = {s.stem for s in mutex_folder.iterdir()}
        config_stems = {s.stem for s in configs}
        both = stems.intersection(config_stems)
        if len(both) > 1:
            raise ValueError(
                f'At most one config file must be loaded from {mutex_folder} to avoid unexpected behavior, instead got {both=}'
            )
        
    with LogLevel(logger=logging.getLogger(), level=logging.CRITICAL):
        gin.parse_config_files_and_bindings(
            configs, 
            bindings=overrides, 
            skip_unknown=skip_unknown
        )

def import_addons(names):
    for name in names:
        try:
            with Suppress():
                bpy.ops.preferences.addon_enable(module=name)
        except Exception:
            logger.warning(f'Could not load addon "{name}"')

@gin.configurable
def configure_render_cycles(

    # supplied by gin.config
    min_samples,
    num_samples,
    time_limit,
    adaptive_threshold,
    exposure,
    denoise
):
    bpy.context.scene.render.engine = 'CYCLES'

    # For now, denoiser is always turned on, but the  _used_
    bpy.context.scene.cycles.use_denoising = denoise
    if denoise:
        try:
            bpy.context.scene.cycles.denoiser = 'OPTIX'
        except Exception as e:
            logger.warning(f"Cannot use OPTIX denoiser {e}")

    bpy.context.scene.cycles.samples = num_samples # i.e. infinity
    bpy.context.scene.cycles.adaptive_min_samples = min_samples
    bpy.context.scene.cycles.adaptive_threshold = adaptive_threshold # i.e. noise threshold
    bpy.context.scene.cycles.time_limit = time_limit
    bpy.context.scene.cycles.film_exposure = exposure
    bpy.context.scene.cycles.volume_step_rate = 0.1
    bpy.context.scene.cycles.volume_preview_step_rate = 0.1
    bpy.context.scene.cycles.volume_max_steps = 32
    bpy.context.scene.cycles.volume_bounces = 4

@gin.configurable
def configure_cycles_devices(
    use_gpu=True
):
    
    if use_gpu is False:
        logger.info(f'Render will use CPU-only due to {use_gpu=}')
        bpy.context.scene.cycles.device = 'CPU'
        return

    assert bpy.context.scene.render.engine == 'CYCLES'
    bpy.context.scene.cycles.device = 'GPU'
    prefs = bpy.context.preferences.addons['cycles'].preferences

    # Necessary to "remind" cycles that the devices exist? Not sure. Without this no devices are found.
    for dt in prefs.get_device_types(bpy.context):
        prefs.get_devices_for_type(dt[0])

    assert len(prefs.devices) != 0, prefs.devices

    types = list(d.type for d in prefs.devices)

    types = sorted(types, key=CYCLES_GPUTYPES_PREFERENCE.index)
    logger.info(f'Available devices have {types=}')
    use_device_type = types[0]

    if use_device_type == 'CPU':
        logger.warning(f'Render will use CPU-only, only found {types=}')
        bpy.context.scene.cycles.device = 'CPU'
        return

    bpy.context.preferences.addons['cycles'].preferences.compute_device_type = use_device_type
    use_devices = [d for d in prefs.devices if d.type == use_device_type]


    logger.info(f'Cycles will use {use_device_type=}, {len(use_devices)=}')

    for d in prefs.devices:
        d.use = False
    for d in use_devices:
        d.use = True

    return use_devices

@gin.configurable
def configure_blender(
    render_engine='CYCLES',
    motion_blur=False,
    motion_blur_shutter=0.5,
):
    bpy.context.preferences.system.scrollback = 0 
    bpy.context.preferences.edit.undo_steps = 0

    if render_engine == 'CYCLES':
        configure_render_cycles()
        configure_cycles_devices()
    else:
        raise ValueError(f'Unrecognized {render_engine=}')

    bpy.context.scene.render.use_motion_blur = motion_blur
    if motion_blur: 
        bpy.context.scene.cycles.motion_blur_position = 'START'
        bpy.context.scene.render.motion_blur_shutter = motion_blur_shutter

    import_addons(['ant_landscape', 'real_snow'])

    