#!/usr/env/python

## Import General Tools
import sys
from pathlib import Path
import logging
import yaml

from datetime import datetime as dt
from datetime import timedelta as tdelta
from time import sleep
import numpy as np
import subprocess
import xml.etree.ElementTree as ET
from astropy.table import Table

from astropy.io import fits

from Instruments import connect_to_ktl, create_log




##-------------------------------------------------------------------------
## MOSFIRE Properties
##-------------------------------------------------------------------------
name = 'MOSFIRE'
serviceNames = ['mosfire', 'mmf1s', 'mmf2s']
modes = ['Dark-Imaging', 'Dark-Spectroscopy', 'Imaging', 'Spectroscopy']
filters = ['Y', 'J', 'H', 'K', 'J2', 'J3', 'NB']

allowed_sampmodes = [2, 3]
sampmode_names = {'CDS': (2, None), 'MCDS': (3, None), 'MCDS16': (3, 16)}

# Load default CSU coordinate transformations
filepath = Path(__file__).parent
with open(filepath.joinpath('MOSFIRE_transforms.txt'), 'r') as FO:
    Aphysical_to_pixel, Apixel_to_physical = yaml.safe_load(FO.read())
Aphysical_to_pixel = np.array(Aphysical_to_pixel)
Apixel_to_physical = np.array(Apixel_to_physical)

log = create_log(name)
services = connect_to_ktl(name, serviceNames)


##-------------------------------------------------------------------------
## Define Common Functions
##-------------------------------------------------------------------------
def get(service, keyword, mode=str):
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
## MOSFIRE Functions
##-------------------------------------------------------------------------
def get_mode():
    '''Get the observing mode and return a two element list: [filter, mode]
    '''
    obsmode = get('mosfire', 'OBSMODE')
    return obsmode.split('-')


def get_filter():
    '''Return the current filter name
    '''
    filter = get('mosfire', 'FILTER')
    return filter


def get_filter1():
    return get('mmf1s', 'posname')


def get_filter2():
    return get('mmf2s', 'posname')


def is_dark():
    '''Return True if the current observing mode is dark
    '''
    filter = get_filter()
    return filter == 'Dark'


def set_mode(filter, mode):
    '''Set the current observing mode to the filter and mode specified.
    '''
    if not mode in modes:
        log.error(f"Mode: {mode} is unknown")
    elif not filter in filters:
        log.error(f"Filter: {filter} is unknown")
    else:
        log.info(f"Setting mode to {filter}-{mode}")
    modestr = f"{filter}-{mode}"
    set('OBSMODE', modestr, wait=True)
    if get_mode() != modestr:
        log.error(f'Mode "{modestr}" not reached.  Current mode: {get_mode()}')


def quick_dark(filter=None):
    '''Modeled after darkeff script
    '''
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
    f1dest = filter_combo.get(filter)[0]
    if get_filter1() != f1dest:
        set('targname', service='mmf1s', f1dest)
    f2dest = filter_combo.get(filter)[1]
    if get_filter2() != f2dest:
        set('targname', service='mmf2s', f2dest)


def go_dark():
    quick_dark()


def grating_shim_ok():
    return get('mosfire', 'MGSSTAT') == 'OK'


def grating_turret_ok():
    return get('mosfire', 'MGTSTAT') == 'OK'


def grating_ok():
    return get('mosfire', 'GRATSTAT') == 'OK'


def filter1_ok():
    return get('mosfire', 'MF1STAT') == 'OK'


def filter2_ok():
    return get('mosfire', 'MF2STAT') == 'OK'


def filters_ok():
    return get('mosfire', 'FILTSTAT') == 'OK'


def fcs_ok():
    return get('mosfire', 'FCSSTAT') == 'OK'


def pupil_rotator_ok():
    return get('mosfire', 'MPRSTAT') == 'OK'


def trapdoor_ok():
    return dustcover_ok()


def dustcover_ok():
    return get('mosfire', 'MDCSTAT') == 'OK'


def check_mechanisms():
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


def set_exptime(exptime):
    '''Set exposure time per coadd in seconds.  Note the ITIME keyword uses ms.
    '''
    set('ITIME', int(exptime*1000))


def set_coadds(coadds):
    '''Set number of coadds
    '''
    set('COADDS', int(coadds))


def parse_sampmode(input):
    if type(input) is int:
        sampmode = input
        numreads = None
    if type(input) is str:
        sampmode, numreads = sampmode_names.get(sampmode)
    return (sampmode, numreads)


def set_sampmode(input):
    sampmode, numreads = parse_sampmode(input)
    if sampmode in allowed_sampmodes:
        log.info(f'Setting Sampling Mode: {sampmode}')
        set('sampmode', sampmode)
        if numreads is not None:
            log.info(f'Setting Number of Reads: {numreads}')
            set('numreads', numreads)
    else:
        log.error(f'Sampling mode {sampmode} is not supported')


def waitfor_exposure(timeout=300):
    done = get('mosfire', 'imagedone', type=bool)
    endat = dt.utcnow() + tdelta(seconds=timeout)
    while done is False and dt.utcnow() < endat:
        sleep(1)
        done = get('mosfire', 'imagedone', type=bool)
    if done is False:
        log.warning(f'Timeout exceeded on waitfor_exposure to finish')
    return done


def goi(exptime=None, coadds=None, sampmode=None):
    waitfor_exposure()
    if exptime is not None:
        set_exptime(exptime)
    if coadds is not None:
        set_coadds(coadds)
    if sampmode is not None:
        set_sampmode(sampmode)
    set('go', 1)


def take_exposure(**kwargs):
    goi(**kwargs)


def filename():
    return Path(get('FILENAME'))


def lastfile():
    lastfile = Path(get('LASTFILE'))
    assert lastfile.exists()
    return lastfile


##-------------------------------------------------------------------------
## Read Mask Design Files
##-------------------------------------------------------------------------
def read_maskfile(xml):
    xmlfile = Path(xml)
    if xmlfile.exists():
        tree = ET.parse(xmlfile)
        root = tree.getroot()
    else:
        try:
            root = ET.fromstring(xml)
        except:
            print(f'Could not parse {xml} as file or XML string')
            raise
    mask = {}
    for child in root:
        if child.tag == 'maskDescription':
            mask['maskDescription'] = child.attrib
        elif child.tag == 'mascgenArguments':
            mask['mascgenArguments'] = {}
            for el in child:
                if el.attrib == {}:
                    mask['mascgenArguments'][el.tag] = (el.text).strip()
                else:
                    print(el.tag, el.attrib)
                    mask['mascgenArguments'][el.tag] = el.attrib
        else:
            mask[child.tag] = [el.attrib for el in child.getchildren()]

    # Combine RA and DEC in to strings, then make table of alignment stars
    for i,star in enumerate(mask.get('alignment')):
        ra = f"{star['targetRaH']}:{star['targetRaM']}:{star['targetRaS']}"
        dec = f"{star['targetDecD']}:{star['targetDecM']}:{star['targetDecS']}"
        mask['alignment'][i]['RA'] = ra
        mask['alignment'][i]['DEC'] = dec
    mask['stars'] = Table(mask.get('alignment'))

    # Combine RA and DEC in to strings, then make table of science targets
    for i,targ in enumerate(mask.get('scienceSlitConfig')):
        ra = f"{targ['targetRaH']}:{targ['targetRaM']}:{targ['targetRaS']}"
        dec = f"{targ['targetDecD']}:{targ['targetDecM']}:{targ['targetDecS']}"
        mask['scienceSlitConfig'][i]['RA'] = ra
        mask['scienceSlitConfig'][i]['DEC'] = dec
    mask['targets'] = Table(mask.get('scienceSlitConfig'))

    return mask


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
def analyze_mask_image(imagefile, filtersize=7):
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
    imagefile = Path(imagefile).abspath
    try:
        hdul = fits.open(imagefile)
        data = hdul[0].data
    except Error as e:
        log.error(e)
        raise
    ## Get image from ginga
#     try:
#         channel = self.fv.get_channel(self.chname)
#         image = channel.get_current_image()
#         data = image._get_data()
#     except:
#         print('Failed to load image data')
#         return

    # median X pixels only (preserve Y structure)
    medimage = ndimage.median_filter(data, size=(1, filtersize))
    
    bars_analysis = {}
    state_analysis = {}
    for slit in range(1,47):
        b1, b2 = slit_to_bars(slit)
        ## Determine y pixel range
        y1 = int(np.ceil((physical_to_pixel(np.array([(4.0, slit+0.5)])))[0][1]))
        y2 = int(np.floor((physical_to_pixel(np.array([(270.4, slit-0.5)])))[0][1]))
        gradx = np.gradient(medimage[y1:y2,:], axis=1)
        horizontal_profile = np.sum(gradx, axis=0)
        x1, x2 = self.find_bar_edges(horizontal_profile)
        if x1 is None:
            self.bars_analysis[b1] = None
            self.state_analysis[b1] = 'UNKNOWN'
        else:
            mm1 = (self.pixel_to_physical(np.array([(x1, (y1+y2)/2.)])))[0][0]
            self.bars_analysis[b1] = mm1
            self.state_analysis[b1] = 'ANALYZED'
        if x2 is None:
            self.bars_analysis[b2] = None
            self.state_analysis[b2] = 'UNKNOWN'
        else:
            mm2 = (self.pixel_to_physical(np.array([(x2, (y1+y2)/2.)])))[0][0]
            self.bars_analysis[b2] = mm2
            self.state_analysis[b2] = 'ANALYZED'
        testx1 = self.physical_to_pixel([[mm2, slit]])
        print(slit, x2, x1, x1-x2, mm2, mm1, testx1)
#         self.compare_to_csu_bar_state()


def find_bar_edges(self, horizontal_profile):
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
## MOSFIRE Quick Checkout
##-------------------------------------------------------------------------
def checkout_quick(interactive=True):
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
    * Create an 2.7x46 long slit and image it, verify bar positions
    * Create an 0.7x46 long slit and image it, verify bar positions
    * With the hatch closed change the observing mode to J-imaging, verify
         mechanisms are ok
    * Quick Dark
    '''
    intromsg = 'This script will do a quick checkout of MOSFIRE.  It should '\
               'take about ?? minutes to complete.  Please confirm that you '\
               'have started the MOSFIRE software AND that the instrument '\
               'rotator is not within 10 degrees of a multiple of 180 degrees.'
    if interactive:
        log.info(intromsg)
        log.info()
        print('Proceed? [y]')
        proceed = input('Continue? [y]')
        if proceed.lower() not in ['y', 'yes', 'ok', '']:
            log.info('Exiting script.')
            return False
        log.info('Executing quick checkout script.')
    
    # Verify that the instrument is "dark"
    if not is_dark():
        go_dark()
    check_mechanisms()
    # Verify Dark Image
    set_exptime(1)
    set_coadds(1)
    set_sampmode('CDS')
    waitfor_exposure() # in case exposure is already in progress
    goi()
    waitfor_exposure()
    hdul = fits.open(lastfile())
    # tests on dark file


    # Create an 2.7x46 long slit and image it, verify bar positions
    # Create an 0.7x46 long slit and image it, verify bar positions
    # Change the observing mode to J-imaging, verify mechanisms
    # Quick Dark
