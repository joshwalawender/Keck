#!/usr/env/python

## Import General Tools
import sys
from pathlib import Path
import logging
import yaml
import random
import re

from datetime import datetime as dt
from datetime import timedelta as tdelta
from time import sleep
import numpy as np
import subprocess
import xml.etree.ElementTree as ET

from astropy.table import Table, Column
from astropy.io import fits
from astropy.coordinates import SkyCoord
from astropy import units as u
from astropy.modeling import models, fitting
from scipy import ndimage

from Instruments import connect_to_ktl, create_log

import matplotlib as mpl
mpl.use('Agg')
from matplotlib import pyplot as plt
from astropy import visualization as viz
plt.ioff()


##-------------------------------------------------------------------------
## MOSFIRE Properties
##-------------------------------------------------------------------------
name = 'MOSFIRE'
serviceNames = ['mosfire', 'mmf1s', 'mmf2s', 'mcsus', 'mfcs', 'mds']
modes = ['dark-imaging', 'dark-spectroscopy', 'imaging', 'spectroscopy']
filters = ['Y', 'J', 'H', 'K', 'J2', 'J3', 'NB']

allowed_sampmodes = [2, 3]
sampmode_names = {'CDS': (2, None), 'MCDS': (3, None), 'MCDS16': (3, 16)}

# Load default CSU coordinate transformations
filepath = Path(__file__).parent
with open(filepath.joinpath('MOSFIRE_transforms.txt'), 'r') as FO:
    Aphysical_to_pixel, Apixel_to_physical = yaml.safe_load(FO.read())
Aphysical_to_pixel = np.array(Aphysical_to_pixel)
Apixel_to_physical = np.array(Apixel_to_physical)

log = create_log(name, loglevel='DEBUG')
services = connect_to_ktl(name, serviceNames)


##-------------------------------------------------------------------------
## MOSFIRE Exceptions
##-------------------------------------------------------------------------
class CSUFatalError(Exception):
    def __init__(self, *args):
        self.args = ('CSU has experienced a Fatal Error.', args)


##-------------------------------------------------------------------------
## Define Mask Object
##-------------------------------------------------------------------------
class Mask(object):
    def __init__(self, input):
        self.slitpos = None
        self.alignmentStars = None
        self.scienceTargets = None
        self.xmlroot = None
        # from maskDescription
        self.name = None
        self.priority = None
        self.center_str = None
        self.center = None
        self.PA = None
        self.mascgenArguments = None

        xmlfile = Path(input)
        # Is the input OPEN mask
        if input.upper() in ['OPEN', 'OPEN MASK']:
            log.debug(f'"{input}" interpreted as OPEN')
            self.build_open_mask()
        elif input.upper() in ['RAND', 'RANDOM']:
            log.debug(f'"{input}" interpreted as RANDOM')
            self.build_random_mask()
        # try top open as XML mask design file
        elif xmlfile.exists():
            log.debug(f'"{input}" exists as file on disk')
            self.read_xml(xmlfile)
        # Try to parse input as long slit specification
        else:
            try:
                width, length = input.split('x')
                width = float(width)
                length = int(length)
                assert length <= 46
                assert width > 0
                self.build_longslit(input)
            except:
                log.debug(f'Unable to parse "{input}" as long slit')
                log.error(f'Unable to parse "{input}"')
                raise ValueError(f'Unable to parse "{input}"')


    def read_xml(self, xml):
        xmlfile = Path(xml)
        if xmlfile.exists():
            tree = ET.parse(xmlfile)
            self.xmlroot = tree.getroot()
        else:
            try:
                self.xmlroot = ET.fromstring(xml)
            except:
                log.error(f'Could not parse {xml} as file or XML string')
                raise
        # Parse XML root
        for child in self.xmlroot:
            if child.tag == 'maskDescription':
                self.name = child.attrib.get('maskName')
                self.priority = float(child.attrib.get('totalPriority'))
                self.PA = float(child.attrib.get('maskPA'))
                self.center_str = f"{child.attrib.get('centerRaH')}:"\
                                  f"{child.attrib.get('centerRaM')}:"\
                                  f"{child.attrib.get('centerRaS')} "\
                                  f"{child.attrib.get('centerDecD')}:"\
                                  f"{child.attrib.get('centerDecM')}:"\
                                  f"{child.attrib.get('centerDecS')}"
                self.center = SkyCoord(self.center_str, unit=(u.hourangle, u.deg))
            elif child.tag == 'mascgenArguments':
                self.mascgenArguments = {}
                for el in child:
                    if el.attrib == {}:
                        self.mascgenArguments[el.tag] = (el.text).strip()
                    else:
                        self.mascgenArguments[el.tag] = el.attrib
            elif child.tag == 'mechanicalSlitConfig':
                data = [el.attrib for el in child.getchildren()]
                self.slitpos = Table(data)
            elif child.tag == 'scienceSlitConfig':
                data = [el.attrib for el in child.getchildren()]
                self.scienceTargets = Table(data)
                ra = [f"{star['targetRaH']}:{star['targetRaM']}:{star['targetRaS']}"
                      for star in self.scienceTargets]
                dec = [f"{star['targetDecD']}:{star['targetDecM']}:{star['targetDecS']}"
                       for star in self.scienceTargets]
                self.scienceTargets.add_columns([Column(ra, name='RA'),
                                                 Column(dec, name='DEC')])
            elif child.tag == 'alignment':
                data = [el.attrib for el in child.getchildren()]
                self.alignmentStars = Table(data)
                ra = [f"{star['targetRaH']}:{star['targetRaM']}:{star['targetRaS']}"
                      for star in self.alignmentStars]
                dec = [f"{star['targetDecD']}:{star['targetDecM']}:{star['targetDecS']}"
                       for star in self.alignmentStars]
                self.alignmentStars.add_columns([Column(ra, name='RA'),
                                                 Column(dec, name='DEC')])
            else:
                mask[child.tag] = [el.attrib for el in child.getchildren()]


    def build_longslit(self, input):
        '''Build a longslit mask
        '''
        # parse input string assuming format similar to 0.7x46
        width, length = input.split('x')
        width = float(width)
        length = int(length)
        assert length <= 46
        self.name = f'LONGSLIT-{input}'
        slits_list = []
        # []
        # scale = 0.7 arcsec / 0.507 mm
        for i in range(length):
            # Convert index iteration to slit number
            # Start with slit number 23 (middle of CSU) and grow it by adding
            # a bar first on one side, then the other
            slitno = int( {0: -1, -1:1}[-1*(i%2)] * (i+(i+1)%2)/2 + 24 )
            leftbar = slitno*2
            leftmm = 145.82707536231888 + -0.17768476719087264*leftbar + (width-0.7)/2*0.507/0.7
            rightbar = slitno*2-1
            rightmm = leftmm - width*0.507/0.7
            slitcent = (slitno-23) * .490454545
            slits_list.append( {'centerPositionArcsec': slitcent,
                                'leftBarNumber': leftbar,
                                'leftBarPositionMM': leftmm,
                                'rightBarNumber': rightbar,
                                'rightBarPositionMM': rightmm,
                                'slitNumber': slitno,
                                'slitWidthArcsec': width,
                                'target': ''} )
        self.slitpos = Table(slits_list)

        # Alignment Box
        slit23 = self.slitpos[self.slitpos['slitNumber'] == 23][0]
        leftmm = slit23['leftBarPositionMM'] - 1.65*0.507/0.7
        rightmm = slit23['rightBarPositionMM'] + 1.65*0.507/0.7
        as_dict = {'centerPositionArcsec': 0.0,
                   'leftBarNumber': 46,
                   'leftBarPositionMM': leftmm,
                   'mechSlitNumber': 23,
                   'rightBarNumber': 45,
                   'rightBarPositionMM': rightmm,
                   'slitWidthArcsec': 4.0,
                   'targetCenterDistance': 0,
                   }
        self.alignmentStars = Table([as_dict])


    def build_open_mask(self):
        '''Build OPEN mask
        '''
        self.name = 'OPEN'
        slits_list = []
        for i in range(46):
            slitno = i+1
            leftbar = slitno*2
            leftmm = 270.400
            rightbar = slitno*2-1
            rightmm = 4.000
            slitcent = 0
            width  = (leftmm-rightmm) * 0.7/0.507
            slits_list.append( {'centerPositionArcsec': slitcent,
                                'leftBarNumber': leftbar,
                                'leftBarPositionMM': leftmm,
                                'rightBarNumber': rightbar,
                                'rightBarPositionMM': rightmm,
                                'slitNumber': slitno,
                                'slitWidthArcsec': width,
                                'target': ''} )
        self.slitpos = Table(slits_list)


    def build_random_mask(self, slitwidth=0.7):
        '''Build a Mask with randomly placed, non contiguous slits
        '''
        self.name = 'RANDOM'
        slits_list = []
        for i in range(46):
            slitno = i+1
            cent = random.randrange(54,220)
            # check if it is the same as the previous slit
            if i > 0:
                while cent == slits_list[i-1]['centerPositionArcsec']:
                    cent = random.randrange(54,220)
            leftbar = slitno*2
            leftmm = cent + slitwidth*0.507/0.7
            rightbar = slitno*2-1
            rightmm = cent - slitwidth*0.507/0.7
            width  = (leftmm-rightmm) * 0.7/0.507
            slits_list.append( {'centerPositionArcsec': cent,
                                'leftBarNumber': leftbar,
                                'leftBarPositionMM': leftmm,
                                'rightBarNumber': rightbar,
                                'rightBarPositionMM': rightmm,
                                'slitNumber': slitno,
                                'slitWidthArcsec': width,
                                'target': ''} )
        self.slitpos = Table(slits_list)
        


##-------------------------------------------------------------------------
## Define Common Functions
##-------------------------------------------------------------------------
def get(keyword, service='mosfire', mode=str):
    """Generic function to get a keyword value.  Converts it to the specified
    mode and does some simple parsing of true and false strings.
    """
    log.debug(f'Querying {service} for {keyword}')
    if services == {}:
        return None
    assert mode in [str, float, int, bool]
    kwresult = services[service][keyword].read()
    log.debug(f'  Got result: "{kwresult}"')

    # Handle string versions of true and false
    if mode is bool:
        if kwresult.strip().lower() == 'false':
            result = False
        elif kwresult.strip().lower() == 'true':
            result = True
        else:
            try:
                result = bool(int(kwresult))
            except:
                result = None
        if result is not None:
            log.debug(f'  Parsed to boolean: {result}')
        else:
            log.error(f'  Failed to parse "{kwresult}"')
        return result
    # Convert result to requested type
    try:
        result = mode(kwresult)
        log.debug(f'  Parsed to {mode}: {result}')
        return result
    except ValueError:
        log.warning(f'Failed to parse {kwresult} as {mode}, returning string')
        return kwresult


def set(keyword, value, service='mosfire', wait=True):
    """Generic function to set a keyword value.
    """
    log.debug(f'Setting {service}.{keyword} to "{value}" (wait={wait})')
    if services == {}:
        return None
    services[service][keyword].write(value, wait=wait)
    log.debug(f'  Done.')


##-------------------------------------------------------------------------
## MOSFIRE Mode and Filter Functions
##-------------------------------------------------------------------------
def obsmode():
    '''Get the observing mode and return a two element list: [filter, mode]
    '''
    obsmode = get('OBSMODE')
    return obsmode


def set_obsmode(obsmode, wait=True, timeout=60):
    '''Set the current observing mode to the filter and mode specified.
    '''
    filter, mode = obsmode.split('-')
    mode = mode.lower()
    log.info(f"Setting mode to {obsmode}")
    if not mode in modes:
        log.error(f"Mode: {mode} is unknown")
    elif not filter in filters:
        log.error(f"Filter: {filter} is unknown")
    else:
        set('SETOBSMODE', obsmode, wait=True)
        if wait is True:
            endat = dt.utcnow() + tdelta(seconds=timeout)
            done = (obsmode().lower() == obsmode.lower())
            while not done and dt.utcnow() < endat:
                sleep(1)
                done = (obsmode().lower() == obsmode.lower())
            if not done:
                log.warning(f'Timeout exceeded on waiting for mode {modestr}')


def filter():
    '''Return the current filter name
    '''
    filter = get('FILTER')
    return filter


def filter1():
    '''Return the current filter name for filter wheel 1
    '''
    return get('posname', service='mmf1s')


def filter2():
    '''Return the current filter name for filter wheel 2
    '''
    return get('posname', service='mmf2s')


def is_dark():
    '''Return True if the current observing mode is dark
    '''
    filter = filter()
    return filter == 'Dark'


def quick_dark(filter=None):
    '''Set the instrument to a dark mode which is close to the specified filter.
    Modeled after darkeff script.
    '''
    log.info('Setting quick dark')
    if filter not in filters:
        log.error(f'Filter {filter} not in allowed filter list: {filters}')
        filter = None
    filter_combo = {'Y': ['H2', 'Y'],
                    'J': ['NB1061', 'J'],
                    'H': ['NB1061', 'H'],
                    'Ks': ['NB1061', 'Ks'],
                    'K': ['NB1061', 'K'],
                    'J2': ['J2', 'K'],
                    'J3': ['J3', 'K'],
                    'H1': ['H1', 'K'],
                    'H2': ['H2', 'K'],
                    None: ['NB1061', 'Ks'],
                    }
    f1dest, f2dest = filter_combo.get(filter)
    if filter1() != f1dest:
        set('targname', f1dest, service='mmf1s')
    if filter2() != f2dest:
        set('targname', f2dest, service='mmf2s')


def go_dark(filter=None, wait=True):
    '''Alias for quick_dark
    '''
    quick_dark(filter=filter)
    if wait is True:
        waitfor_dark()


def waitfor_dark(timeout=300, noshim=False):
    log.debug('Waiting for dark')
    endat = dt.utcnow() + tdelta(seconds=timeout)
    if noshim is False:
        sleep(1)
    while is_dark() is False and dt.utcnow() < endat:
        sleep(1)
    if is_dark() is False:
        log.warning(f'Timeout exceeded on waitfor_dark to finish')
    return is_dark()


##-------------------------------------------------------------------------
## MOSFIRE Status Check Functions
##-------------------------------------------------------------------------
def grating_shim_ok():
    return get('MGSSTAT') == 'OK'


def grating_turret_ok():
    return get('MGTSTAT') == 'OK'


def grating_ok():
    return get('GRATSTAT') == 'OK'


def filter1_ok():
    return get('MF1STAT') == 'OK'


def filter2_ok():
    return get('MF2STAT') == 'OK'


def filters_ok():
    return get('FILTSTAT') == 'OK'


def fcs_ok():
    return get('FCSSTAT') == 'OK'


def pupil_rotator_ok():
    return get('MPRSTAT') in ['OK', 'Tracking']


def trapdoor_ok():
    return dustcover_ok()


def dustcover_ok():
    return get('MDCSTAT') == 'OK'


def check_mechanisms():
    log.info('Checking mechanisms')
    mechs = ['filter1', 'filter2', 'fcs', 'grating_shim', 'grating_turret',
             'pupil_rotator', 'trapdoor']
    for mech in mechs:
        statusfn = getattr(sys.modules[__name__], f'{mech}_ok')
        ok = statusfn()
        if ok is False:
            log.error(f'{mech} status is not ok')
            log.error(f'Please address the problem, then re-run the checkout.')
            return False
    return True


##-------------------------------------------------------------------------
## MOSFIRE Exposure Control Functions
##-------------------------------------------------------------------------
def exptime():
    return get('EXPTIME', mode=int)/1000


def set_exptime(exptime):
    '''Set exposure time per coadd in seconds.  Note the ITIME keyword uses ms.
    '''
    log.info(f'Setting exposure time to {int(exptime*1000)} ms')
    set('ITIME', int(exptime*1000))


def coadds():
    return get('COADDS', mode=int)


def set_coadds(coadds):
    '''Set number of coadds
    '''
    log.info(f'Setting number of coadds to {int(coadds)}')
    set('COADDS', int(coadds))


def sampmode():
    return get('SAMPMODE', mode=int)


def parse_sampmode(input):
    if type(input) is int:
        sampmode = input
        numreads = None
    if type(input) is str:
        sampmode, numreads = sampmode_names.get(input)
    return (sampmode, numreads)


def set_sampmode(input):
    log.info(f'Setting Sampling Mode to {input}')
    sampmode, numreads = parse_sampmode(input)
    if sampmode in allowed_sampmodes:
        log.debug(f'Setting Sampling Mode to: {sampmode}')
        set('sampmode', sampmode)
        if numreads is not None:
            log.debug(f'Setting Number of Reads: {numreads}')
            set('numreads', numreads)
    else:
        log.error(f'Sampling mode {sampmode} is not supported')


def waitfor_exposure(timeout=300, noshim=False):
    log.debug('Waiting for exposure to finish')
    if noshim is False:
        sleep(1)
    done = get('imagedone', mode=bool) and get('ready', service='mds', mode=bool)
    endat = dt.utcnow() + tdelta(seconds=timeout)
    while done is False and dt.utcnow() < endat:
        sleep(1)
        done = get('imagedone', mode=bool) and get('ready', service='mds', mode=bool)
    if done is False:
        log.warning(f'Timeout exceeded on waitfor_exposure to finish')
    return done


def wfgo(timeout=300, noshim=False):
    '''Alias waitfor_exposure to wfgo
    '''
    waitfor_exposure(timeout=timeout, noshim=noshim)


def goi(exptime=None, coadds=None, sampmode=None):
    waitfor_exposure(noshim=True)
    if exptime is not None:
        set_exptime(exptime)
    if coadds is not None:
        set_coadds(coadds)
    if sampmode is not None:
        set_sampmode(sampmode)
    log.info('Taking exposure')
    set('go', '1')


def take_exposure(exptime=None, coadds=None, sampmode=None):
    goi(exptime=exptime, coadds=coadds, sampmode=sampmode)


def filename():
    return Path(get('FILENAME'))


def lastfile():
    lastfile = Path(get('LASTFILE'))
    if lastfile.exists():
        return lastfile
    else:
        # Check and see if we need a /s prepended on the path for this machine
        trypath = Path('/s')
        for part in lastfile.parts[1:]:
            trypath = trypath.joinpath(part)
        if not trypath.exists():
            log.warning(f'Could not find last file on disk: {lastfile}')
        else:
            return trypath


def waitfor_FCS(timeout=60, PAthreshold=0.1, ELthreshold=0.1, noshim=False):
    '''Wait for FCS to get close to actual PA and EL.
    '''
    log.debug('Waiting for FCS to reach destination')
    if noshim is False:
        sleep(1)
    telPA = get('PA', service='mfcs', mode=float)
    telEL = get('EL', service='mfcs', mode=float)
    fcsPA, fcsEL = get('PA_EL', service='mfcs', mode=str).split()
    fcsPA = float(fcsPA)
    fcsEL = float(fcsEL)
    PAdiff = abs(fcsPA - telPA)
    ELdiff = abs(fcsEL - telEL)
    done = (PAdiff < PAthreshold) and (ELdiff < ELthreshold)
    endat = dt.utcnow() + tdelta(seconds=timeout)
    while done is False and dt.utcnow() < endat:
        sleep(1)
        done = (PAdiff < PAthreshold) and (ELdiff < ELthreshold)
    if done is False:
        log.warning(f'Timeout exceeded on waitfor_FCS to finish')
    return done


##-------------------------------------------------------------------------
## CSU Controls
##-------------------------------------------------------------------------
def CSUready():
    ready = get('CSUREADY', mode=int)
    translation = {0: 'Unknown',
                   1: 'System Started',
                   2: 'Ready for Move',
                   3: 'Moving',
                   4: 'Configuring',
                   -1: 'Error',
                   -2: 'System Stopped'}
    log.debug(f'  CSU state: {ready}, {translation[ready]}')
    if ready == -1:
        raise CSUFatalError
    return ready


def execute_mask():
    '''Execute a mask which has already been set up.
    '''
    log.info('Executing CSU move')
    set('CSUGO', 1)
    sleep(3) # shim needed because CSUREADY keyword doesn't update fast enough


def waitfor_CSU(timeout=480, noshim=False):
    '''Wait for a CSU move to be complete.
    '''
    log.debug('Waiting for CSU to be ready')
    if noshim is False:
        sleep(1)
    done = CSUready() == 2 # 2 is 'Ready for Move'
    endat = dt.utcnow() + tdelta(seconds=timeout)
    while done is False and dt.utcnow() < endat:
        sleep(2)
        done = CSUready() == 2 # 2 is 'Ready for Move'
    if done is False:
        log.warning(f'Timeout exceeded on waitfor_CSU to finish')
    return done


def setup_mask(mask):
    '''Setup the given mask.  Accepts a Mask object.
    '''
    if type(mask) != Mask:
        log.error(f"Input {mask} is not a Mask object")
        return False
    # Now setup the mask
    log.info(f'Setting up mask: {mask.name}')
    log.debug('Setting bar target position keywords')
    for slit in mask.slitpos:
        log.debug(f"  Setting B{slit['rightBarNumber']:02d}TARG = {slit['rightBarPositionMM']}")
        set(f"B{slit['rightBarNumber']:02d}TARG", slit['rightBarPositionMM'])
        log.debug(f"  Setting B{slit['leftBarNumber']:02d}TARG = {slit['leftBarPositionMM']}")
        set(f"B{slit['leftBarNumber']:02d}TARG", slit['leftBarPositionMM'])
    log.debug('Invoke SETUP process on CSU')
    set('CSUSETUP', 1)
    set('SETUPNAME', mask.name, service='mcsus')
    while get('CSUSTAT', service='mcsus') == 'Creating Group.':
        sleep(1)
    csustatus = get('CSUSTAT', service='mcsus')
    if re.search('Setup aborted.  Collision detected at row (\d+)', csustatus):
        log.error(csustatus)


def initialise_bars(bars=None):
    if bars is None:
        log.info('Initializing all bars')
        set('CSUINITBAR', 0)
    else:
        if type(bars) == int:
            assert bar >= 0
            assert bar <= 46
            log.info('Initializing bar {bar}')
            set('CSUINITBAR', bar)
        else:
            for bar in bars:
                assert type(bar) == int
                assert bar >= 0
                assert bar <= 46
                log.info('Initializing bar {bar}')
                set('CSUINITBAR', bar)


## ------------------------------------------------------------------
##  Coordinate Transformation Utilities
## ------------------------------------------------------------------
def slit_to_bars(slit):
    '''Given a slit number (1-46), return the two bar numbers associated
    with that slit.
    '''
    return (slit*2-1, slit*2)


def bar_to_slit(bar):
    '''Given a bar number, retun the slit associated with that bar.
    '''
    return int((bar+1)/2)


def pad(x):
    '''Pad array for affine transformation.
    '''
    return np.hstack([x, np.ones((x.shape[0], 1))])


def unpad(x):
    '''Unpad array for affine transformation.
    '''
    return x[:,:-1]


def fit_transforms(pixels, physical):
    '''Given a set of pixel coordinates (X, Y) and a set of physical
    coordinates (mm, slit), fit the affine transformations (forward and
    backward) to convert between the two coordinate systems.
    
    '''
    pixels = np.array(pixels)
    physical = np.array(physical)
    assert pixels.shape[1] == 2
    assert physical.shape[1] == 2
    assert pixels.shape[0] == physical.shape[0]

    # Pad the data with ones, so that our transformation can do translations too
    n = pixels.shape[0]
    pad = lambda x: np.hstack([x, np.ones((x.shape[0], 1))])
    unpad = lambda x: x[:,:-1]
    X = pad(pixels)
    Y = pad(physical)

    # Solve the least squares problem X * A = Y
    # to find our transformation matrix A
    A, res, rank, s = np.linalg.lstsq(X, Y, rcond=None)
    Ainv, res, rank, s = np.linalg.lstsq(Y, X, rcond=None)
    A[np.abs(A) < 1e-10] = 0
    Ainv[np.abs(A) < 1e-10] = 0
    Apixel_to_physical = A
    Aphysical_to_pixel = Ainv
    return Apixel_to_physical, Aphysical_to_pixel


def pixel_to_physical(x):
    '''Using the affine transformation determined by `fit_transforms`,
    convert a set of pixel coordinates (X, Y) to physical coordinates (mm,
    slit).
    '''
    x = np.array(x)
    result = unpad(np.dot(pad(x), Apixel_to_physical))
    return result


def physical_to_pixel(x):
    '''Using the affine transformation determined by `fit_transforms`,
    convert a set of physical coordinates (mm, slit) to pixel coordinates
    (X, Y).
    '''
    x = np.array(x)
    result = unpad(np.dot(pad(x), Aphysical_to_pixel))
    return result


## Set up initial transforms for pixel and physical space
# pixelfile = filepath.joinpath('MOSFIRE_pixels.txt')
# with open(pixelfile, 'r') as FO:
#     contents = FO.read()
#     pixels = yaml.safe_load(contents)
# physicalfile = filepath.joinpath('MOSFIRE_physical.txt')
# with open(physicalfile, 'r') as FO:
#     contents = FO.read()
#     physical = yaml.safe_load(contents)
# Apixel_to_physical, Aphysical_to_pixel = fit_transforms(pixels, physical)
## Convert from numpy arrays to list for simpler YAML
# Apixel_to_physical = [ [float(val) for val in line] for line in Apixel_to_physical]
# Aphysical_to_pixel = [ [float(val) for val in line] for line in Aphysical_to_pixel]
# with open('MOSFIRE_transforms.txt', 'w') as FO:
#     FO.write(yaml.dump([Aphysical_to_pixel, Apixel_to_physical]))


## ------------------------------------------------------------------
##  Analyze Image to Determine Bar Positions
## ------------------------------------------------------------------
def analyze_mask_image(imagefile, filtersize=7, plot=False):
    '''Loop over all slits in the image and using the affine transformation
    determined by `fit_transforms`, select the Y pixel range over which this
    slit should be found.  Take a median filtered version of that image and
    determine the X direction gradient (derivative).  Then collapse it in
    the Y direction to form a 1D profile.
    
    Using the `find_bar_edges` method, determine the X pixel positions of
    each bar forming the slit.
    
    Convert those X pixel position to physical coordinates using the
    `pixel_to_physical` method and then call the `compare_to_csu_bar_state`
    method to determine the bar state.
    '''
    ## Get image from file
    imagefile = Path(imagefile).absolute()
    try:
        hdul = fits.open(imagefile)
        data = hdul[0].data
    except Error as e:
        log.error(e)
        raise
    # median X pixels only (preserve Y structure)
    medimage = ndimage.median_filter(data, size=(1, filtersize))
    
    bars = {}
    ypos = {}
    for slit in range(1,47):
        b1, b2 = slit_to_bars(slit)
        ## Determine y pixel range
        y1 = int(np.ceil((physical_to_pixel(np.array([(4.0, slit+0.5)])))[0][1]))
        y2 = int(np.floor((physical_to_pixel(np.array([(270.4, slit-0.5)])))[0][1]))
        ypos[b1] = [y1, y2]
        ypos[b2] = [y1, y2]
        gradx = np.gradient(medimage[y1:y2,:], axis=1)
        horizontal_profile = np.sum(gradx, axis=0)
        bars[b1], bars[b2] = find_bar_edges(horizontal_profile)

    # Generate plot if called for
    if plot is True:
        plotfile = imagefile.with_name(f"{imagefile.name}.png")
        if plotfile.exists(): plotfile.unlink()
        plt.figure(figsize=(16,16))
        norm = viz.ImageNormalize(data, interval=viz.PercentileInterval(99),
                                  stretch=viz.LinearStretch())
        plt.imshow(data, norm=norm, origin='lower', cmap='Greys')
        for bar in bars.keys():
            plt.plot([0,2048], [ypos[bar][0], ypos[bar][0]], 'r-', alpha=0.1)
            plt.plot([0,2048], [ypos[bar][1], ypos[bar][1]], 'r-', alpha=0.1)
            plt.plot(bars[bar], np.mean(ypos[bar]), 'rx', alpha=0.5)
        plt.savefig(str(plotfile))

    return bars


def find_bar_edges(horizontal_profile):
    '''Given a 1D profile, dertermime the X position of each bar that forms
    a single slit.  The slit edges are found by fitting one positive and
    one negative gaussian function to the profile.
    '''
    fitter = fitting.LevMarLSQFitter()

    amp1_est = horizontal_profile[horizontal_profile == min(horizontal_profile)]
    mean1_est = np.argmin(horizontal_profile)
    amp2_est = horizontal_profile[horizontal_profile == max(horizontal_profile)]
    mean2_est = np.argmax(horizontal_profile)

    g_init1 = models.Gaussian1D(amplitude=amp1_est, mean=mean1_est, stddev=2.)
    g_init1.amplitude.max = 0
    g_init2 = models.Gaussian1D(amplitude=amp2_est, mean=mean2_est, stddev=2.)
    g_init2.amplitude.min = 0

    model = g_init1 + g_init2
    fit = fitter(model, range(0,horizontal_profile.shape[0]), horizontal_profile)

    # Check Validity of Fit
    if abs(fit.stddev_0.value) < 3 and abs(fit.stddev_1.value) < 3\
       and fit.amplitude_0.value < -1 and fit.amplitude_1.value > 1\
       and fit.mean_0.value > fit.mean_1.value:
        x1 = fit.mean_0.value
        x2 = fit.mean_1.value
    else:
        x1 = None
        x2 = None
    
    return (x1, x2)



##-------------------------------------------------------------------------
## MOSFIRE Checkout
##-------------------------------------------------------------------------
def checkout(quick=False):
    '''
    * Confirm the physical drive angle. It should not be within 10 degrees of a
         multiple of 180 degrees
    * Start the observing software as moseng or the account for the night
    * Check that the dark filter is selected. If not select it
    * Check mechanism status: If any of the mechanisms have a big red X on it,
         you will need to home mechanisms. Note, if filter wheel is at the home
         position, Status will be "OK," position will be "HOME", target will be
         "unknown", and there will still be a big red X.
    * Acquire an exposure
    * Inspect the dark image
    * If normal checkout:
        * Open mask
        * Image and confirm
        * Initialize CSU: modify -s mosfire csuinitbar=0
        * Image and confirm
        * Form 0.7x46 long slit
        * Image and confirm
    * If quick checkout
        * Form an 2.7x46 long slit
        * Image and confirm
        * Form an 0.7x46 long slit
        * Image and confirm
    * With the hatch closed change the observing mode to J-imaging, verify
         mechanisms are ok
    * Quick Dark
    * Message user to verify sidecar logging
    '''
    intromsg = 'This script will do a quick checkout of MOSFIRE.  It should '\
               'take about ?? minutes to complete.  Please confirm that you '\
               'have started the MOSFIRE software AND that the instrument '\
               'rotator is not within 10 degrees of a multiple of 180 degrees.'
    log.info(intromsg)
    print()
    proceed = input('Continue? [y]')
    if proceed.lower() not in ['y', 'yes', 'ok', '']:
        log.info('Exiting script.')
        return False
    log.info(f'Executing checkout script.')
    
    log.info('Checking that instrument is dark')
    if not is_dark():
        go_dark()
        waitfor_dark()
    if not is_dark():
        log.error('Could not make instrument dark')
        return False
    log.info('Instrument is dark')

    log.info('Checking mechanisms')
    if check_mechanisms() is True:
        log.info('  Mechanisms ok')
    else:
        log.error('  Mechanism check failed')
        return False

    log.info('Taking dark image')
    set_exptime(2)
    set_coadds(1)
    set_sampmode('CDS')
    sleep(5)
    take_exposure()
    waitfor_exposure()

    log.info(f'Please verify that {lastfile()} looks normal for a dark image')
    proceed = input('Continue? [y]')
    if proceed.lower() not in ['y', 'yes', 'ok', '']:
        log.critical('Exiting script.')
        return False

    # Quick checkout
    if quick is True:
        log.info('Setup 2.7x46 long slit mask')
        setup_mask(Mask('2.7x46'))
        waitfor_CSU()
        log.info('Execute mask')
        execute_mask()
        waitfor_CSU()
        set_obsmode('K-imaging', wait=True)
        take_exposure()
        waitfor_exposure()
        wideSlitFile = lastfile()
        go_dark()

        log.info('Setup 0.7x46 long slit mask')
        setup_mask(Mask('0.7x46'))
        waitfor_CSU()
        log.info('Execute mask')
        execute_mask()
        waitfor_CSU()
        set_obsmode('K-imaging', wait=True)
        take_exposure()
        waitfor_exposure()
        wideSlitFile = lastfile()
        go_dark()


    # Normal (long) checkout
    if quick is False:
        log.info('Setup OPEN mask')
        setup_mask(Mask('OPEN'))
        waitfor_CSU()
        execute_mask()
        waitfor_CSU()

