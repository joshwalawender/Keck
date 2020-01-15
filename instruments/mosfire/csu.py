import numpy as np
from astropy.table import Table, Column, Row

from .core import *
from .mechs import *
from .mask import *

class CSUFatalError(Exception):
    def __init__(self, *args):
        self.args = ('CSU has experienced a Fatal Error.', args)


##-------------------------------------------------------------------------
## CSU Controls
##-------------------------------------------------------------------------
def barok(barnum):
    '''Is bar number barnum ok?
    '''
    return get(f"B{barnum:02d}STAT") == 'OK'


def CSUok():
    '''Are all the bars ok?
    '''
    barstats = [barok(barnum) for barnum in range(1,93,1)]
    return np.all(barstats)


def CSUready():
    '''Is the CSU in the ready state?
    '''
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
        rbn = slit['rightBarNumber']
        rbp = slit['rightBarPositionMM']
        lbn = slit['leftBarNumber']
        lbp = slit['leftBarPositionMM']
        log.debug(f"  Setting B{rbn:02d}TARG = {rbp}")
        set(f"B{rbn:02d}TARG", rbp)
        log.debug(f"  Setting B{lbn:02d}TARG = {lbp}")
        set(f"B{lbn:02d}TARG", lbp)
    log.debug('Invoke SETUP process on CSU')
    set('CSUSETUP', 1)
    set('SETUPNAME', mask.name, service='mcsus')
    while get('CSUSTAT', service='mcsus') == 'Creating Group.':
        sleep(1)
    csustatus = get('CSUSTAT', service='mcsus')
    if re.search('Setup aborted.  Collision detected at row (\d+)', csustatus):
        log.error(csustatus)


def initialise_bars(bars=None):
    '''Initialize one or more CSU bars.
    '''
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


def get_current_mask():
    '''
    '''
    barpos = [get(f"B{bar:02d}POS", mode=float) for bar in range(1,93,1)]
    bartarg = [get(f"B{bar:02d}TARG", mode=float) for bar in range(1,93,1)]
    deltas = [abs(pair[0]-pair[1]) < 0.01 for pair in zip(barpos, bartarg)]
    assert np.all(deltas)

    current_mask = Mask(None)
    current_mask.name = get('MASKNAME', service='mcsus')

    slits_list = []
    for slitno in range(1,47,1):
        leftbar = slitno*2
        leftmm = barpos[leftbar-1]
        rightbar = slitno*2-1
        rightmm = barpos[rightbar-1]
        centermm = (leftmm + rightmm) / 2
        slitcent = 189.62934431020133 - 1.3801254681363402 * centermm
        width = (leftmm - rightmm)*0.7/0.507
        slits_list.append( {'centerPositionArcsec': slitcent,
                            'leftBarNumber': leftbar,
                            'leftBarPositionMM': leftmm,
                            'rightBarNumber': rightbar,
                            'rightBarPositionMM': rightmm,
                            'slitNumber': slitno,
                            'slitWidthArcsec': width,
                            'target': ''} )
    current_mask.slitpos = Table(slits_list)
    return current_mask


##-------------------------------------------------------------------------
## Bar Status Checks
##-------------------------------------------------------------------------
def read_csu_bar_state():
    if not csu_bar_state_file.exists():
        log.error(f"Unable to locate csu_bar_state file: {csu_bar_state_file}")
        return None
    mask = Mask(None)
    mask.name = 'From csu_bar_state'
    with open(csu_bar_state_file, 'r') as cbs:
        lines = cbs.readlines()
    t = Table(names=('slitNumber', 'leftBarNumber', 'rightBarNumber',
                     'leftBarPositionMM', 'rightBarPositionMM',
                     'centerPositionArcsec', 'slitWidthArcsec', 'target'),
              dtype=('i4', 'i4', 'i4', 'f4', 'f4', 'f4', 'f4', 'a30'))
    for line in lines:
        barno, barpos, barstate = line.strip('\n').split(',')
        slit = bar_to_slit(int(barno))
        if int(barno) % 2 != 0:
            dict = {'slitNumber': slit}
            dict['leftBarNumber'] = -1
            dict['leftBarPositionMM'] = float('nan')
            dict['rightBarNumber'] = int(barno)
            dict['rightBarPositionMM'] = float(barpos)
            dict['centerPositionArcsec'] = float('nan')
            dict['slitWidthArcsec'] = float('nan')
            dict['target'] = ''
            t.add_row(dict)
        else:
            t[slit-1]['leftBarNumber'] = int(barno)
            t[slit-1]['leftBarPositionMM'] = float(barpos)
    mask.slitpos = t
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
# Apixel_to_physical = [[float(val) for val in l] for l in Apixel_to_physical]
# Aphysical_to_pixel = [[float(val) for val in l] for l in Aphysical_to_pixel]
# with open('MOSFIRE_transforms.txt', 'w') as FO:
#     FO.write(yaml.dump([Aphysical_to_pixel, Apixel_to_physical]))