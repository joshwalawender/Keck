#!kpython3

## Import General Tools
import inspect
from datetime import datetime, timedelta
from time import sleep
from pathlib import Path
import configparser

from .core import *
from .mask import Mask
from .filter import go_dark
from .obsmode import set_obsmode
from .csu import setup_mask, execute_mask
from .hatch import open_hatch, close_hatch
from .detector import take_exposure
from .domelamps import dome_flat_lamps
from .power import Ne_lamp, Ar_lamp


##-------------------------------------------------------------------------
## Sub-function: Read Configuration
##-------------------------------------------------------------------------
def read_calibration_config(input):
    cfg = configparser.ConfigParser()
    if input is None:
        log.debug('Using default config file')
        cfg.read(mosfire_data_file_path.joinpath('default_calibrations.cfg'))
    elif isinstance(input, str):
        input = Path(input)
        cfg.read(input)
    elif isinstance(input, Path):
        cfg.read(input)
    elif isinstance(input, dict):
        cfg.read_dict(input)
    else:
        raise FailedCondition(f"Unable to interpret {input} as configuration")
    return cfg


##-------------------------------------------------------------------------
## Sub-function: Take Arcs
##-------------------------------------------------------------------------
def take_arcs(filt, cfg):
    # Take Ne arcs
    nNeArcs = cfg[filt].getint("ne_arc_count", 0)
    if nNeArcs > 0:
        log.info(f'Taking {nNeArcs:d} Ne arcs')
        # Close hatch
        go_dark()
        close_hatch()
        set_obsmode(f"{filt}-spectroscopy")
        exptime = cfg[filt].getfloat("ne_arc_exptime", 2)
        Ne_lamp('on')
        for i in range(cfg[filt].getint("ne_arc_count", 0)):
            take_exposure(exptime=exptime,
                          coadds=cfg[filt].getint("ne_arc_coadds", 1),
                          sampmode=cfg[filt].get("ne_arc_sampmode", 'CDS'),
                          object='Ne arc',
                          wait=True)
        Ne_lamp('off')
    # Take Ar arcs
    nArArcs = cfg[filt].getint("ar_arc_count", 0)
    if nArArcs > 0:
        log.info(f'Taking {nArArcs:d} Ar arcs')
        # Close hatch
        
        go_dark()
        close_hatch()
        set_obsmode(f"{filt}-spectroscopy")
        exptime = cfg[filt].getfloat("ar_arc_exptime", 2)
        Ar_lamp('on')
        for i in range(cfg[filt].getint("ar_arc_count", 0)):
            take_exposure(exptime=exptime,
                          coadds=cfg[filt].getint("ar_arc_coadds", 1),
                          sampmode=cfg[filt].get("ar_arc_sampmode", 'CDS'),
                          object='Ar arc',
                          wait=True)
        Ar_lamp('off')
    log.info('Going dark')
    go_dark()


##-------------------------------------------------------------------------
## Sub-function: Take Flats
##-------------------------------------------------------------------------
def take_flats(filt, cfg, imaging=False, lampsoff=False):

    if imaging is False:
        config = cfg[filt]
    elif imaging is True:
        config = cfg[f"{filt}-imaging"]

    # Get number
    if lampsoff is False:
        nflats = config.getint("flat_count", 0)
        lamps_string = ''
    elif lampsoff is True:
        nflats = config.getint("flatoff_count", 0)
        lamps_string = ' (lamps off)'

    exptime = config.getfloat("flat_exptime", 11)
    if nflats > 0:
        log.info(f'Taking {nflats} flats{lamps_string}')
        # Open Hatch
        open_hatch()
        # Turn on dome flat lamps
        if lampsoff is False:
            dome_flat_lamps(config.getfloat('flat_power'))
        elif lampsoff is True:
            dome_flat_lamps('off')
        # Set mode
        if imaging is False:
            set_obsmode(f"{filt}-spectroscopy")
        elif imaging is True:
            set_obsmode(f"{filt}-imaging")
        # Take flats
        for i in range(nflats):
            log.info(f"Taking flat {i+1}/{nflats} (exptime = {exptime:.0f})")
            take_exposure(exptime=exptime,
                          coadds=config.getint("flat_coadds", 1),
                          sampmode=config.get("flat_sampmode", 'CDS'),
                          object=f'Dome Flat{lamps_string}',
                          wait=True)

    log.info('Going dark')
    go_dark()


##-------------------------------------------------------------------------
## Take Calibrations for a Single Mask for a List of Bands
##-------------------------------------------------------------------------
def take_calibrations_for_a_mask(mask, filters, cfg, imaging=False,
                                 skipprecond=False, skippostcond=True):
    '''Takes calibrations for a single mask in a list of filters.
    '''
    this_script_name = inspect.currentframe().f_code.co_name
    log.debug(f"Executing: {this_script_name}")

    ##-------------------------------------------------------------------------
    ## Pre-Condition Checks
    if skipprecond is True:
        log.debug('Skipping pre condition checks')
    else:
        mechanisms_ok()
        # Mask input is a mask object
        if not isinstance(mask, Mask):
            mask = Mask(mask)
        # Input filters is list
        if type(filters) is str:
            filters = [filters]
        # Filters are in congifuration
        for filt in filters:
            if imaging is False and filt not in cfg.keys():
                raise FailedCondition(f'Filter "{filt}" not in configuration')
            if imaging is True and f"{filt}-imaging" not in cfg.keys():
                raise FailedCondition(f'Filter "{filt}-imaging" not in configuration')

    ##-------------------------------------------------------------------------
    ## Script Contents
    imstring = {False: '', True: ' imaging'}[imaging]
    log.info(f'Taking{imstring} calibrations for {mask.name} in {", ".join(filters)}')

    # Go Dark
    go_dark()
    # Configure CSU
    setup_mask(mask)
    execute_mask()

    for filt in filters:
        hatch_posname = ktl.cache(service='mmdcs', keyword='POSNAME')
        if hatch_posname.read() == 'Closed':
            # Start with Arcs
            if imaging is False: take_arcs(filt, cfg)
            take_flats(filt, cfg, imaging=imaging)
            take_flats(filt, cfg, imaging=imaging, lampsoff=True)
        elif hatch_posname.read() == 'Open':
            # Start with Flats
            take_flats(filt, cfg, imaging=imaging)
            take_flats(filt, cfg, imaging=imaging, lampsoff=True)
            if imaging is False: take_arcs(filt, cfg)
        else:
            raise FailedCondition(f'Hatch in unknown state: "{hatch_posname}"')

    log.info(f'Done with {", ".join(filters)} calibrations for "{mask.name}"')

    ##-------------------------------------------------------------------------
    ## Post-Condition Checks
    if skippostcond is True:
        log.debug('Skipping post condition checks')
    else:
        mechanisms_ok()

    return None


##-------------------------------------------------------------------------
## Take Calibrations for All Masks
##-------------------------------------------------------------------------
def take_calibrations(filters, config=None, imaging=False,
                      skipprecond=False, skippostcond=True):
    '''Loops over masks and takes calibrations for each.
    
    All masks must have the same calibration configuration file.
    
    Takes an input dictionary containing keys which are Mask objects (or
    resolve to mask objects), and values which are a list of filters.
    '''
    this_function_name = inspect.currentframe().f_code.co_name
    log.debug(f"Executing: {this_function_name}")

    ##-------------------------------------------------------------------------
    ## Pre-Condition Checks
    if skipprecond is True:
        log.debug('Skipping pre condition checks')
    else:
        pass

    ##-------------------------------------------------------------------------
    ## Script Contents
    cfg = read_calibration_config(config)

    # Iterate over masks and take cals
    for i,mask in enumerate(filters.keys()):
        log.info(f"Taking calibrations for mask {i+1}/{len(filters)}")
        take_calibrations_for_a_mask(mask, filters[mask], cfg, imaging=imaging,
                                     skipprecond=skipprecond,
                                     skippostcond=skippostcond)
    dome_flat_lamps('off')

    ##-------------------------------------------------------------------------
    ## Post-Condition Checks
    if skippostcond is True:
        log.debug('Skipping post condition checks')
    else:
        pass

    return None


# if __name__ == '__main__':
#     import argparse
#     ## Parse Command Line Arguments
#     p = argparse.ArgumentParser(description=description)
#     ## add options
#     p.add_argument("-c", "--config", dest="config", type=str,
#         default=None,
#         help="The configuration file to use.")
#     args = p.parse_args()
