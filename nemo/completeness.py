"""

Tools for calculating completeness of cluster samples.

"""

import os
import sys
import resource
import glob
import numpy as np
import pylab as plt
import astropy.table as atpy
from astLib import *
from scipy import stats
from scipy import interpolate
from scipy.interpolate import InterpolatedUnivariateSpline as _spline
from scipy import ndimage
from scipy import optimize
from . import signals
from . import maps
from . import MockSurvey
from . import plotSettings
from . import startUp
from collections import OrderedDict
import colorcet
import types
import pickle
import astropy.io.fits as pyfits
import time
import IPython
plt.matplotlib.interactive(False)

# If want to catch warnings as errors...
#import warnings
#warnings.filterwarnings('error')

#------------------------------------------------------------------------------------------------------------
class SelFn(object):
        
    def __init__(self, parDictFileName, selFnDir, SNRCut, footprintLabel = None, zStep = 0.01, 
                 enableDrawSample = False, downsampleRMS = True, applyMFDebiasCorrection = True,
                 setUpAreaMask = False, enableCompletenessCalc = True):
        """Initialise a SelFn object.
        
        This is a class that uses the output from nemoSelFn to re-calculate the selection function
        (completeness fraction on (M, z) grid) with different cosmological / scaling relation parameters
        (see SelFn.update).
        
        Use footprintLabel to specify a footprint (e.g., 'DES', 'HSC', 'KiDS' etc.) defined in 
        selFnFootprints in the .yml config file. The default None uses the whole survey.
        
        Set downsampleRMS = True to speed up the completeness calculation (called each time update is called)
        considerably.
        
        """
        
        self.SNRCut=SNRCut
        self.footprintLabel=footprintLabel
        self.downsampleRMS=downsampleRMS
        self.applyMFDebiasCorrection=applyMFDebiasCorrection
        self.selFnDir=selFnDir

        self.tckQFitDict=signals.loadQ(self.selFnDir+os.path.sep+"QFit.fits")
        parDict=startUp.parseConfigFile(parDictFileName)
        self.tileNames=self.tckQFitDict.keys()
        
        # ignoreMPI gives us the complete list of tileNames, regardless of how this parameter is set in the config file
        #config=startUp.NemoConfig(parDictFileName, makeOutputDirs = False, ignoreMPI = True)
        #parDict=config.parDict
        #self.tileNames=config.tileNames
        
        # Sanity check that any given footprint is defined - if not, give a useful error message
        if footprintLabel is not None:
            if 'selFnFootprints' not in parDict.keys():
                raise Exception("No footprints defined in .yml config file")
            else:
                labelsList=[]
                for footprintDict in parDict['selFnFootprints']:
                    labelsList.append(footprintDict['label'])
                if footprintLabel not in labelsList:
                    raise Exception("Footprint '%s' not found in selFnFootprints - check .yml config file" % (footprintLabel))
        
        # We only care about the filter used for fixed_ columns
        self.photFilterLabel=['photFilter']
        
        self.scalingRelationDict=parDict['massOptions']
        
        # Load area masks
        if setUpAreaMask == True:
            # Takes around 20 sec
            self._setUpAreaMask()
        else:
            self.tileTab=None
            self.WCSDict=None
            self.areaMaskDict=None
        
        # Tile-weighted average noise and area will not change... we'll just re-calculate fitTab and put in place here
        # Takes around 20 sec
        self.selFnDictList=[]
        self.totalAreaDeg2=0.0
        for tileName in self.tileNames:
            RMSTab=getRMSTab(tileName, self.photFilterLabel, self.selFnDir, footprintLabel = self.footprintLabel)
            if type(RMSTab) == atpy.Table:
                tileAreaDeg2=RMSTab['areaDeg2'].sum()
                if tileAreaDeg2 > 0:
                    if downsampleRMS == True:
                        RMSTab=downsampleRMSTab(RMSTab)
                    selFnDict={'tileName': tileName,
                            'RMSTab': RMSTab,
                            'tileAreaDeg2': tileAreaDeg2}
                    self.selFnDictList.append(selFnDict)
                    self.totalAreaDeg2=self.totalAreaDeg2+tileAreaDeg2
        
        # Set initial fiducial cosmology - can be overridden using update function
        if enableCompletenessCalc == True:
            minMass=5e13
            zMin=0.0
            zMax=2.0
            H0=70.
            Om0=0.30
            Ob0=0.05
            sigma_8=0.8
            self.mockSurvey=MockSurvey.MockSurvey(minMass, self.totalAreaDeg2, zMin, zMax, H0, Om0, Ob0, sigma_8, zStep = zStep, enableDrawSample = enableDrawSample)
            # An initial run...
            self.update(H0, Om0, Ob0, sigma_8)


    def _setUpAreaMask(self):
        """Sets-up WCS info and loads area masks - needed for quick position checks etc.
        
        """
        
        # This takes ~20 sec to set-up - we could cache, or it's overhead when initialising SelFn
        # We could do a lot of this by just making the area mask, mass limit maps etc. MEFs
        # But then we wouldn't want to lazily load them (probably)
        self.tileTab=atpy.Table()
        self.tileTab.add_column(atpy.Column(list(self.tileNames), 'tileName'))
        self.tileTab.add_column(atpy.Column(np.zeros(len(self.tileNames)), 'RAMin'))
        self.tileTab.add_column(atpy.Column(np.zeros(len(self.tileNames)), 'RAMax'))
        self.tileTab.add_column(atpy.Column(np.zeros(len(self.tileNames)), 'decMin'))
        self.tileTab.add_column(atpy.Column(np.zeros(len(self.tileNames)), 'decMax'))
        self.WCSDict={}
        self.areaMaskDict={}
        for row in self.tileTab:
            areaMap, wcs=loadAreaMask(row['tileName'], self.selFnDir)
            self.WCSDict[row['tileName']]=wcs.copy()
            self.areaMaskDict[row['tileName']]=areaMap
            ra0, dec0=self.WCSDict[row['tileName']].pix2wcs(0, 0)
            ra1, dec1=self.WCSDict[row['tileName']].pix2wcs(wcs.header['NAXIS1'], wcs.header['NAXIS2'])
            if ra1 > ra0:
                ra1=-(360-ra1)
            row['RAMin']=min([ra0, ra1])
            row['RAMax']=max([ra0, ra1])
            row['decMin']=min([dec0, dec1])
            row['decMax']=max([dec0, dec1])
            
            
    def checkCoordsInAreaMask(self, RADeg, decDeg):
        """Checks if the given RA, dec coords are in valid regions of the map. 
        
        Args:
            RADeg (float or numpy array): RA in decimal degrees
            decDeg (float or numpy array): dec in decimal degrees
        
        Returns:
            True if in the area mask mask, False if not
            
        """

        if self.tileTab is None:
            self._setUpAreaMask()
                    
        RADeg=np.array(RADeg)
        decDeg=np.array(decDeg)
        if RADeg.shape == ():
            RADeg=[RADeg]
        if decDeg.shape == ():
            decDeg=[decDeg]
        inMaskList=[]
        for ra, dec in zip(RADeg, decDeg):
            inMask=False
            # Inside footprint check
            raMask=np.logical_and(np.greater_equal(ra, self.tileTab['RAMin']), np.less(ra, self.tileTab['RAMax']))
            decMask=np.logical_and(np.greater_equal(dec, self.tileTab['decMin']), np.less(dec, self.tileTab['decMax']))
            tileMask=np.logical_and(raMask, decMask)
            # This is just dealing with bytes versus strings in python3
            matchTilesList=[]
            for item in self.tileTab['tileName'][tileMask].tolist():
                if type(item) == bytes:
                    matchTilesList.append(item.decode('utf-8'))
                else:
                    matchTilesList.append(str(item))
            for tileName in matchTilesList:
                x, y=self.WCSDict[tileName].wcs2pix(ra, dec)
                if self.areaMaskDict[tileName][int(y), int(x)] > 0:
                    inMask=True
            inMaskList.append(inMask)
        
        if len(inMaskList) > 1:
            return np.array(inMaskList)
        else:
            return inMaskList[0]            
            

    def overrideRMS(self, RMS, obsFreqGHz = 148.0):
        """Override the RMS noise of the SelFn object - replacing it with constant value RMS (given in uK/arcmin^2).
        This is translated into a y0~ noise level at the given observing frequency.
        
        After doing this call update() to get an updated (M, z) completeness grid.
        
        """
        
        for selFnDict in self.selFnDictList:
            print("Add RMS override code")
            IPython.embed()
            sys.exit()            
        

    def update(self, H0, Om0, Ob0, sigma_8, scalingRelationDict = None):
        """Re-calculates survey-average selection function given new set of cosmological / scaling relation parameters.
        
        This updates self.mockSurvey at the same time - i.e., this is the only thing that needs to be updated.
        
        Resulting (M, z) completeness grid stored as self.compMz
        
        To apply the selection function and get the expected number of clusters in the survey do e.g.:
        
        selFn.mockSurvey.calcNumClustersExpected(selFn = selFn.compMz)
        
        (yes, this is a bit circular)
        
        """
        
        if scalingRelationDict != None:
            self.scalingRelationDict=scalingRelationDict
        
        self.mockSurvey.update(H0, Om0, Ob0, sigma_8)
        
        tileAreas=[]
        compMzCube=[]
        for selFnDict in self.selFnDictList:
            tileAreas.append(selFnDict['tileAreaDeg2'])
            selFnDict['compMz']=calcCompleteness(selFnDict['RMSTab'], self.SNRCut, selFnDict['tileName'], self.mockSurvey, 
                                                            self.scalingRelationDict, self.tckQFitDict)
            compMzCube.append(selFnDict['compMz'])
        tileAreas=np.array(tileAreas)
        fracArea=tileAreas/self.totalAreaDeg2
        compMzCube=np.array(compMzCube)
        self.compMz=np.average(compMzCube, axis = 0, weights = fracArea)
                    

    def projectCatalogToMz(self, tab):
        """Project a catalog (as astropy Table) into the (log10 M500, z) grid. Takes into account the uncertainties
        on y0, redshift - but if redshift error is non-zero, is a lot slower.
                
        Returns (log10 M500, z) grid
        
        """
        
        catProjectedMz=np.zeros(self.mockSurvey.clusterCount.shape)
        tenToA0, B0, Mpivot, sigma_int=self.scalingRelationDict['tenToA0'], self.scalingRelationDict['B0'], \
                                       self.scalingRelationDict['Mpivot'], self.scalingRelationDict['sigma_int']

        for row in tab:
            tileName=row['template'].split("#")[-1]
            z=row['redshift']
            zErr=row['redshiftErr']
            y0=row['fixed_y_c']*1e-4
            y0Err=row['fixed_err_y_c']*1e-4
            P=signals.calcPM500(y0, y0Err, z, zErr, self.tckQFitDict[tileName], self.mockSurvey, 
                                  tenToA0 = tenToA0, B0 = B0, Mpivot = Mpivot, sigma_int = sigma_int, 
                                  applyMFDebiasCorrection = self.applyMFDebiasCorrection, fRelWeightsDict = {148.0: 1.0},
                                  return2D = True)
            # Paste into (M, z) grid
            catProjectedMz=catProjectedMz+P # For return2D = True, P is normalised such that 2D array sum is 1
        
        return catProjectedMz
    
#------------------------------------------------------------------------------------------------------------
def loadAreaMask(tileName, selFnDir):
    """Loads the survey area mask (i.e., after edge-trimming and point source masking, produced by nemo) for
    the given tile.
    
    Returns map array, wcs
    
    """
    
    areaMap, wcs=_loadTile(tileName, selFnDir, "areaMask", extension = 'fits')   
    return areaMap, wcs

#------------------------------------------------------------------------------------------------------------
def loadRMSMap(tileName, selFnDir, photFilter):
    """Loads the RMS map for the given tile.
    
    Returns map array, wcs
    
    """
    
    RMSMap, wcs=_loadTile(tileName, selFnDir, "RMSMap_%s" % (photFilter), extension = 'fits')   
    return RMSMap, wcs

#------------------------------------------------------------------------------------------------------------
def loadMassLimitMap(tileName, diagnosticsDir, z):
    """Loads the mass limit map for the given tile at the given z.
    
    Returns map array, wcs
    
    """
    
    massLimMap, wcs=_loadTile(tileName, diagnosticsDir, "massLimitMap_z%s" % (str(z).replace(".", "p")), 
                              extension = 'fits')   
    return massLimMap, wcs

#------------------------------------------------------------------------------------------------------------
def loadIntersectionMask(tileName, selFnDir, footprint):
    """Loads the intersection mask for the given tile and footprint.
    
    Returns map array, wcs
    
    """           
            
    intersectMask, wcs=_loadTile(tileName, selFnDir, "intersect_%s" % (footprint), extension = 'fits')   
    return intersectMask, wcs

#------------------------------------------------------------------------------------------------------------
def _loadTile(tileName, baseDir, baseFileName, extension = 'fits'):
    """Generic function to load a tile image from either a multi-extension FITS file, or a file with 
    #tileName.fits type extension, whichever is found.
        
    Returns map array, wcs
    
    """
    
    # After tidyUp is run, this will be a MEF file... during first run, it won't be (written under MPI)
    if os.path.exists(baseDir+os.path.sep+"%s#%s.%s" % (baseFileName, tileName, extension)):
        fileName=baseDir+os.path.sep+"%s#%s.%s" % (baseFileName, tileName, extension)
    else:
        fileName=baseDir+os.path.sep+"%s.%s" % (baseFileName, extension)
    with pyfits.open(fileName) as img:
        # Handling compressed images when we're not actually using tiles
        if img[tileName].data is None and tileName == 'PRIMARY':
            tileName='COMPRESSED_IMAGE'
        data=img[tileName].data
        wcs=astWCS.WCS(img[tileName].header, mode = 'pyfits')
    
    return data, wcs

#------------------------------------------------------------------------------------------------------------
def getTileTotalAreaDeg2(tileName, selFnDir, masksList = [], footprintLabel = None):
    """Returns total area of the tile pointed at by tileName (taking into account survey mask and point
    source masking).
    
    A list of other survey masks (e.g., from optical surveys like KiDS, HSC) can be given in masksList. 
    These should be file names for .fits images where 1 defines valid survey area, 0 otherwise. If given,
    this routine will return the area of intersection between the extra masks and the SZ survey.
            
    """
    
    areaMap, wcs=loadAreaMask(tileName, selFnDir)
    areaMapSqDeg=(maps.getPixelAreaArcmin2Map(areaMap, wcs)*areaMap)/(60**2)
    totalAreaDeg2=areaMapSqDeg.sum()
    
    if footprintLabel != None:  
        intersectMask=makeIntersectionMask(tileName, selFnDir, footprintLabel, masksList = masksList)
        totalAreaDeg2=(areaMapSqDeg*intersectMask).sum()        
        
    return totalAreaDeg2

#------------------------------------------------------------------------------------------------------------
def makeIntersectionMask(tileName, selFnDir, label, masksList = []):
    """Creates intersection mask between mask files given in masksList.
    
    Calculating the intersection is slow (~30 sec per tile per extra mask for KiDS), so intersection masks 
    are cached; label is used in output file names (e.g., diagnosticsDir/intersect_label#tileName.fits)
    
    Can optionally be called without extraMasksList, IF the intersection mask has already been created and
    cached.
    
    NOTE: We assume masks have dec aligned with y direction for speed.
    
    Returns intersectionMask as array (1 = valid area, 0 = otherwise)
    
    """
    
    # After tidyUp has run, there will be intersection mask MEF files
    intersectFileName=selFnDir+os.path.sep+"intersect_%s.fits" % (label)
    if os.path.exists(intersectFileName):
        intersectMask, wcs=loadIntersectionMask(tileName, selFnDir, label)
        return intersectMask
    
    # Otherwise, we may have a per-tile intersection mask
    intersectFileName=selFnDir+os.path.sep+"intersect_%s#%s.fits" % (label, tileName)
    if os.path.exists(intersectFileName):
        intersectMask, wcs=loadIntersectionMask(tileName, selFnDir, label)
        return intersectMask

    # Otherwise... make it
    areaMap, wcs=loadAreaMask(tileName, selFnDir)
    RAMin, RAMax, decMin, decMax=wcs.getImageMinMaxWCSCoords()
    if masksList == []:
        raise Exception("didn't find previously cached intersection mask but makeIntersectionMask called with empty masksList")
    print("... creating %s intersection mask (%s) ..." % (label, tileName))         
    intersectMask=np.zeros(areaMap.shape)
    outRACoords=np.array(wcs.pix2wcs(np.arange(intersectMask.shape[1]), [0]*intersectMask.shape[1]))
    outDecCoords=np.array(wcs.pix2wcs([0]*np.arange(intersectMask.shape[0]), np.arange(intersectMask.shape[0])))
    outRA=outRACoords[:, 0]
    outDec=outDecCoords[:, 1]
    RAToX=interpolate.interp1d(outRA, np.arange(intersectMask.shape[1]), fill_value = 'extrapolate')
    DecToY=interpolate.interp1d(outDec, np.arange(intersectMask.shape[0]), fill_value = 'extrapolate')
    for fileName in masksList:
        with pyfits.open(fileName) as maskImg:
            for hdu in maskImg:
                if type(hdu) == pyfits.ImageHDU:
                    break
            maskWCS=astWCS.WCS(hdu.header, mode = 'pyfits')
            maskData=hdu.data
        # From sourcery tileDir stuff
        RAc, decc=wcs.getCentreWCSCoords()
        xc, yc=maskWCS.wcs2pix(RAc, decc)
        xc, yc=int(xc), int(yc)
        xIn=np.arange(maskData.shape[1])
        yIn=np.arange(maskData.shape[0])
        inRACoords=np.array(maskWCS.pix2wcs(xIn, [yc]*len(xIn)))
        inDecCoords=np.array(maskWCS.pix2wcs([xc]*len(yIn), yIn))
        inRA=inRACoords[:, 0]
        inDec=inDecCoords[:, 1]
        RAToX=interpolate.interp1d(inRA, xIn, fill_value = 'extrapolate')
        DecToY=interpolate.interp1d(inDec, yIn, fill_value = 'extrapolate')
        outRACoords=np.array(wcs.pix2wcs(np.arange(intersectMask.shape[1]), [0]*intersectMask.shape[1]))
        outDecCoords=np.array(wcs.pix2wcs([0]*np.arange(intersectMask.shape[0]), np.arange(intersectMask.shape[0])))
        outRA=outRACoords[:, 0]
        outDec=outDecCoords[:, 1]
        xIn=np.array(RAToX(outRA), dtype = int)
        yIn=np.array(DecToY(outDec), dtype = int)
        xMask=np.logical_and(xIn >= 0, xIn < maskData.shape[1])
        yMask=np.logical_and(yIn >= 0, yIn < maskData.shape[0])
        xOut=np.arange(intersectMask.shape[1])
        yOut=np.arange(intersectMask.shape[0])
        for i in yOut[yMask]:
            intersectMask[i][xMask]=maskData[yIn[i], xIn[xMask]]
    intersectMask=np.array(np.greater(intersectMask, 0.5), dtype = int)
    maps.saveFITS(intersectFileName, intersectMask, wcs, compressed = True)
        
    return intersectMask

#------------------------------------------------------------------------------------------------------------
def getRMSTab(tileName, photFilterLabel, selFnDir, diagnosticsDir = None, footprintLabel = None):
    """Makes a table containing map area in tile pointed to by tileName against RMS values (so this compresses
    the information in the RMS maps). The first time this is run takes ~200 sec (for a 1000 sq deg tile), 
    but the result is cached.
    
    If making the RMSTab, diagnosticsDir must be given. If the RMSTab exists, it is not needed.
    
    Can optionally take extra masks for specifying e.g. HSC footprint. Here, we assume these have already
    been made by makeIntersectionMask, and we can load them from the cache, identifying them through
    footprintLabel
        
    Returns RMSTab
    
    """
    
    # After tidyUp has run, we may have one global RMS table with an extra tileName column we can use
    RMSTabFileName=selFnDir+os.path.sep+"RMSTab.fits"
    if footprintLabel is not None:
        RMSTabFileName=RMSTabFileName.replace(".fits", "_%s.fits" % (footprintLabel))
    if os.path.exists(RMSTabFileName):
        tab=atpy.Table().read(RMSTabFileName)
        return tab[np.where(tab['tileName'] == tileName)]
    
    # Otherwise, we may have a per-tile RMS table
    RMSTabFileName=selFnDir+os.path.sep+"RMSTab_%s.fits" % (tileName)
    if footprintLabel != None:
        RMSTabFileName=RMSTabFileName.replace(".fits", "_%s.fits" % (footprintLabel))
    if os.path.exists(RMSTabFileName) == True:
        return atpy.Table().read(RMSTabFileName)
        
    # We can't make the RMSTab without access to stuff in the diagnostics dir
    # So this bit allows us to skip missing RMSTabs for footprints with no overlap (see SelFn)
    if diagnosticsDir is None:
        return None

    print(("... making %s ..." % (RMSTabFileName)))
    RMSMap, wcs=loadRMSMap(tileName, selFnDir, photFilterLabel)
    areaMap, wcs=loadAreaMask(tileName, selFnDir)
    areaMapSqDeg=(maps.getPixelAreaArcmin2Map(areaMap, wcs)*areaMap)/(60**2)

    if footprintLabel != None:  
        intersectMask=makeIntersectionMask(tileName, selFnDir, footprintLabel)
        areaMapSqDeg=areaMapSqDeg*intersectMask        
        RMSMap=RMSMap*intersectMask

    RMSValues=np.unique(RMSMap[np.nonzero(RMSMap)])

    totalAreaDeg2=areaMapSqDeg.sum()
    
    tileArea=np.zeros(len(RMSValues))
    for i in range(len(RMSValues)):
        tileArea[i]=areaMapSqDeg[np.equal(RMSMap, RMSValues[i])].sum()
    RMSTab=atpy.Table()
    RMSTab.add_column(atpy.Column(tileArea, 'areaDeg2'))
    RMSTab.add_column(atpy.Column(RMSValues, 'y0RMS'))
    RMSTab.write(RMSTabFileName)

    return RMSTab

#------------------------------------------------------------------------------------------------------------
def downsampleRMSTab(RMSTab, stepSize = 0.001*1e-4):
    """Downsamples the RMSTab in terms of noise resolution, binning by stepSize.
        
    Returns RMSTab
    
    """
    
    stepSize=0.001*1e-4
    binEdges=np.arange(RMSTab['y0RMS'].min(), RMSTab['y0RMS'].max()+stepSize, stepSize)
    y0Binned=[]
    tileAreaBinned=[]
    binMins=[]
    binMaxs=[]
    for i in range(len(binEdges)-1):
        mask=np.logical_and(RMSTab['y0RMS'] > binEdges[i], RMSTab['y0RMS'] <= binEdges[i+1])
        if mask.sum() > 0:
            y0Binned.append(np.average(RMSTab['y0RMS'][mask], weights = RMSTab['areaDeg2'][mask]))
            tileAreaBinned.append(np.sum(RMSTab['areaDeg2'][mask]))
            binMins.append(binEdges[i])
            binMaxs.append(binEdges[i+1])
    newRMSTab=atpy.Table()
    newRMSTab.add_column(atpy.Column(y0Binned, 'y0RMS'))
    newRMSTab.add_column(atpy.Column(tileAreaBinned, 'areaDeg2'))
    RMSTab=newRMSTab
        
    return RMSTab

#------------------------------------------------------------------------------------------------------------
def calcTileWeightedAverageNoise(tileName, photFilterLabel, selFnDir, diagnosticsDir = None, footprintLabel = None):
    """Returns weighted average noise value in the tile.
    
    """

    RMSTab=getRMSTab(tileName, photFilterLabel, selFnDir, diagnosticsDir = diagnosticsDir, 
                     footprintLabel = footprintLabel)
    RMSValues=np.array(RMSTab['y0RMS'])
    tileArea=np.array(RMSTab['areaDeg2'])
    tileRMSValue=np.average(RMSValues, weights = tileArea)

    return tileRMSValue

#------------------------------------------------------------------------------------------------------------
def completenessByFootprint(selFnCollection, mockSurvey, diagnosticsDir, additionalLabel = ""):
    """Write out average (M, z) grid for all tiles (tileNames) given in selFnCollection (a dictionary with 
    keys corresponding to footprints: 'full' is the entire survey), weighted by fraction of total survey area
    within the footprint. We also produce a bunch of other stats and plots to do with completeness versus 
    redshift.
    
    Output is written to files named e.g. diagnosticsDir/MzCompleteness_label.npz, where label is the 
    footprint (key in selFnCollection); 'full' is the default (survey-wide average).
    
    additionalLabel is optional and will be added to the output filenames (use for e.g., tagging with the
    calcCompleteness method used).
    
    """
        
    zBinEdges=np.arange(0.05, 2.1, 0.1)
    zBinCentres=(zBinEdges[:-1]+zBinEdges[1:])/2.
        
    for footprintLabel in selFnCollection.keys():
        print(">>> Survey-averaged results inside footprint: %s ..." % (footprintLabel))
        selFnDictList=selFnCollection[footprintLabel]
        tileAreas=[]
        compMzCube=[]
        completeness=[]
        for selFnDict in selFnDictList:
            tileAreas.append(selFnDict['tileAreaDeg2'])
            massLimit_90Complete=calcMassLimit(0.9, selFnDict['compMz'], mockSurvey, zBinEdges = zBinEdges)
            completeness.append(massLimit_90Complete)
            compMzCube.append(selFnDict['compMz'])
        tileAreas=np.array(tileAreas)
        completeness=np.array(completeness)
        if np.sum(tileAreas) == 0:
            print("... no overlapping area with %s ..." % (footprintLabel))
            continue
        fracArea=tileAreas/np.sum(tileAreas)
        compMzCube=np.array(compMzCube)
        compMz_surveyAverage=np.average(compMzCube, axis = 0, weights = fracArea)

        outFileName=diagnosticsDir+os.path.sep+"MzCompleteness_%s%s.npz" % (footprintLabel, additionalLabel)
        np.savez(outFileName, z = mockSurvey.z, log10M500c = mockSurvey.log10M, 
                 M500Completeness = compMz_surveyAverage)
        
        makeMzCompletenessPlot(compMz_surveyAverage, mockSurvey.log10M, mockSurvey.z, footprintLabel, 
                               diagnosticsDir+os.path.sep+"MzCompleteness_%s%s.pdf" % (footprintLabel, additionalLabel))

        # 90% mass completeness limit and plots
        massLimit_90Complete=np.average(completeness, axis = 0, weights = fracArea)  # agrees with full mass limit map
        makeMassLimitVRedshiftPlot(massLimit_90Complete, zBinCentres, diagnosticsDir+os.path.sep+"completeness90Percent_%s%s.pdf" % (footprintLabel, additionalLabel), title = "footprint: %s" % (footprintLabel))
        zMask=np.logical_and(zBinCentres >= 0.2, zBinCentres < 1.0)
        averageMassLimit_90Complete=np.average(massLimit_90Complete[zMask])
        print("... total survey area (after masking) = %.1f sq deg" % (np.sum(tileAreas)))
        print("... survey-averaged 90%% mass completeness limit (z = 0.5) = %.1f x 10^14 MSun" % (massLimit_90Complete[np.argmin(abs(zBinCentres-0.5))]))
        print("... survey-averaged 90%% mass completeness limit (0.2 < z < 1.0) = %.1f x 10^14 MSun" % (averageMassLimit_90Complete))

#------------------------------------------------------------------------------------------------------------
def makeMzCompletenessPlot(compMz, log10M, z, title, outFileName):
    """Makes a (M, z) plot. Here, compMz is a 2d array, and log10M and z are arrays corresponding to the axes.
    
    """
    
    plotSettings.update_rcParams()
    plt.figure(figsize=(9.5,6.5))
    ax=plt.axes([0.11, 0.11, 0.87, 0.80])

    plt.imshow((compMz*100).transpose(), cmap = colorcet.m_rainbow, origin = 'lower', aspect = 0.8)
    
    y_tck=interpolate.splrep(log10M, np.arange(log10M.shape[0]))
    plot_log10M=np.linspace(13.5, 15.5, 9)
    coords_log10M=interpolate.splev(plot_log10M, y_tck)
    labels_log10M=[]
    for lm in plot_log10M:
        labels_log10M.append("%.2f" % (lm))
    plt.yticks(interpolate.splev(plot_log10M, y_tck), labels_log10M)
    plt.ylim(coords_log10M.min(), coords_log10M.max())
    plt.ylabel("log$_{10}$ ($M / M_{\odot}$)")
    
    x_tck=interpolate.splrep(z, np.arange(z.shape[0]))
    plot_z=np.linspace(0.0, 2.0, 11)
    coords_z=interpolate.splev(plot_z, x_tck)
    labels_z=[]
    for lz in plot_z:
        labels_z.append("%.1f" % (lz))
    plt.xticks(interpolate.splev(plot_z, x_tck), labels_z)
    plt.xlim(coords_z.min(), coords_z.max())
    plt.xlabel("$z$")
    
    plt.colorbar(pad = 0.03)
    cbLabel="Completeness (%)" 
    plt.figtext(0.96, 0.52, cbLabel, ha="center", va="center", family = "sans-serif", rotation = "vertical")

    plt.title(title)
    plt.savefig(outFileName)

#------------------------------------------------------------------------------------------------------------
def calcMassLimit(completenessFraction, compMz, mockSurvey, zBinEdges = []):
    """Given a completeness (log10M, z) grid as made by calcCompleteness, return the mass limit (units of 
    10^14 MSun) as a function of redshift. By defualt, the same binning used in the given mockSurvey object -
    this can be overridden by giving zBinEdges.
    
    """
    
    massLimit=np.power(10, mockSurvey.log10M[np.argmin(abs(compMz-completenessFraction), axis = 1)])/1e14
    
    if zBinEdges != []:
        binnedMassLimit=np.zeros(len(zBinEdges)-1)
        for i in range(len(zBinEdges)-1):
            binnedMassLimit[i]=np.average(massLimit[np.logical_and(mockSurvey.z > zBinEdges[i], mockSurvey.z <= zBinEdges[i+1])])
        massLimit=binnedMassLimit
    
    return massLimit
    
#------------------------------------------------------------------------------------------------------------
def calcCompleteness(RMSTab, SNRCut, tileName, mockSurvey, scalingRelationDict, tckQFitDict,
                     plotFileName = None, z = None, method = "fast", numDraws = 2000000, numIterations = 100):
    """Calculate completeness as a function of (log10 M500, z) on the mockSurvey grid for assumed fiducial
    cosmology and scaling relation, at the given SNRCut and noise level. Intrinsic scatter in the scaling
    relation is taken into account.
    
    If plotFileName is given, writes 90% completeness plot.
    
    Two methods for doing the calculation are available:
    - "fast"        : applying measurement error and scatter to 'true' y0 on a grid to get (log10 M, z) 
                      completeness 
    - "Monte Carlo" : using samples drawn from mockSurvey to fill out the (log10 M, z) grid
    
    Adjust numDraws (per iteration) and numIterations to set the noise level of estimate when using the
    "Monte Carlo" method. For the "fast" method, numDraws and numIterations are ignored.
    
    The calculation can be done at a single fixed z, if given.

    Returns 2d array of (log10 M500, z) completeness
    
    """
        
    if z != None:
        zIndex=np.argmin(abs(mockSurvey.z-z))
        zRange=mockSurvey.z[zIndex:zIndex+1]
    else:
        zRange=mockSurvey.z
    
    if method == "Monte Carlo":
        # Monte-carlo sims approach: slow - but can use to verify the other approach below
        halfBinWidth=(mockSurvey.log10M[1]-mockSurvey.log10M[0])/2.0
        binEdges_log10M=(mockSurvey.log10M-halfBinWidth).tolist()+[np.max(mockSurvey.log10M)+halfBinWidth]
        halfBinWidth=(mockSurvey.z[1]-mockSurvey.z[0])/2.0
        binEdges_z=(zRange-halfBinWidth).tolist()+[np.max(zRange)+halfBinWidth]
        allMz=np.zeros([mockSurvey.clusterCount.shape[1], mockSurvey.clusterCount.shape[0]])
        detMz=np.zeros([mockSurvey.clusterCount.shape[1], mockSurvey.clusterCount.shape[0]])
        for i in range(numIterations):
            tab=mockSurvey.drawSample(y0Noise, scalingRelationDict, tckQFitDict, tileName = tileName, 
                                    SNRLimit = SNRCut, applySNRCut = False, z = z, numDraws = numDraws)
            allMz=allMz+np.histogram2d(np.log10(tab['true_M500']*1e14), tab['redshift'], [binEdges_log10M, binEdges_z])[0]
            detMask=np.greater(tab['fixed_y_c']*1e-4, y0Noise*SNRCut)
            detMz=detMz+np.histogram2d(np.log10(tab['true_M500'][detMask]*1e14), tab['redshift'][detMask], [binEdges_log10M, binEdges_z])[0]
        mask=np.not_equal(allMz, 0)
        compMz=np.ones(detMz.shape)
        compMz[mask]=detMz[mask]/allMz[mask]
        compMz=compMz.transpose()
        #astImages.saveFITS("test_compMz_MC_5000.fits", compMz.transpose(), None)

    elif method == "fast":
        
        # Using full noise distribution, weighted by fraction of area
        # NOTE: removed recMassBias and div parameters
        #t0=time.time()
        tenToA0, B0, Mpivot, sigma_int=[scalingRelationDict['tenToA0'], scalingRelationDict['B0'], 
                                        scalingRelationDict['Mpivot'], scalingRelationDict['sigma_int']]
        y0Grid=np.zeros([zRange.shape[0], mockSurvey.clusterCount.shape[1]])
        for i in range(len(zRange)):
            zk=zRange[i]
            k=np.argmin(abs(mockSurvey.z-zk))
            theta500s_zk=interpolate.splev(mockSurvey.log10M, mockSurvey.theta500Splines[k])
            Qs_zk=interpolate.splev(theta500s_zk, tckQFitDict[tileName])
            fRels_zk=interpolate.splev(mockSurvey.log10M, mockSurvey.fRelSplines[k])
            true_y0s_zk=tenToA0*np.power(mockSurvey.Ez[k], 2)*np.power(np.power(10, mockSurvey.log10M)/Mpivot, 1+B0)*Qs_zk*fRels_zk
            #true_y0s_zk=tenToA0*np.power(mockSurvey.Ez[k], 2)*np.power((recMassBias*np.power(10, mockSurvey.log10M))/Mpivot, 1+B0)*Qs_zk*fRels_zk
            y0Grid[i]=true_y0s_zk
            
        # For some cosmological parameters, we can still get the odd -ve y0
        y0Grid[y0Grid <= 0] = 1e-9
        
        # Calculate completeness using area-weighted average
        # NOTE: RMSTab that is fed in here can be downsampled in noise resolution for speed
        areaWeights=RMSTab['areaDeg2']/RMSTab['areaDeg2'].sum()
        log_y0Lim=np.log(SNRCut*RMSTab['y0RMS'])
        log_y0=np.log(y0Grid)
        compMz=np.zeros(log_y0.shape)
        for i in range(len(RMSTab)):
            SNRGrid=y0Grid/RMSTab['y0RMS'][i]
            SNRGrid=SNRGrid
            log_y0Err=1/SNRGrid
            log_y0Err[SNRGrid < SNRCut]=1/SNRCut
            log_totalErr=np.sqrt(log_y0Err**2 + sigma_int**2)
            compMz=compMz+stats.norm.sf(log_y0Lim[i], loc = log_y0, scale = log_totalErr)*areaWeights[i]
        #t1=time.time()
        
        # For sanity checking figure-of-merit
        #predMz=compMz*mockSurvey.clusterCount
        #predMz=predMz/predMz.sum()
        #astImages.saveFITS("predMz.fits", predMz.transpose(), None)        
        #projImg=pyfits.open("projMz_SNR%.2f.fits" % (SNRCut))        
        #projMz=projImg[0].data.transpose()
        #projImg.close()
        #merit=np.sum(np.sqrt(np.power(projMz-predMz, 2)))
        #print(merit)
        #IPython.embed()
        #sys.exit()
            
    else:
        raise Exception("calcCompleteness only has 'fast', and 'Monte Carlo' methods available")
            
    if plotFileName != None:
        # Calculate 90% completeness as function of z
        zBinEdges=np.arange(0.05, 2.1, 0.1)
        zBinCentres=(zBinEdges[:-1]+zBinEdges[1:])/2.
        massLimit_90Complete=calcMassLimit(0.9, compMz, mockSurvey, zBinEdges = zBinEdges)
        zMask=np.logical_and(zBinCentres >= 0.2, zBinCentres < 1.0)
        averageMassLimit_90Complete=np.average(massLimit_90Complete[zMask])
        makeMassLimitVRedshiftPlot(massLimit_90Complete, zBinCentres, plotFileName, 
                                   title = "%s: $M_{\\rm 500c}$ / $10^{14}$ M$_{\odot}$ > %.2f (0.2 < $z$ < 1)" % (tileName, averageMassLimit_90Complete)) 
            
    return compMz
      
#------------------------------------------------------------------------------------------------------------
def makeMassLimitMap(SNRCut, z, tileName, photFilterLabel, mockSurvey, scalingRelationDict, tckQFitDict, 
                     diagnosticsDir, selFnDir):
    """Makes a map of 90% mass completeness (for now, this fraction is fixed).
    
    NOTE: The map here is "downsampled" (i.e., binned) in terms of noise resolution - okay for display 
    purposes, may not be for analysis using the map (if anyone should decide to). Much quicker and saves
    on memory issues (the y0 noise estimates have uncertainties themselves anyway).
    
    """
        
    RMSMap, wcs=loadRMSMap(tileName, selFnDir, photFilterLabel)
    RMSTab=getRMSTab(tileName, photFilterLabel, selFnDir)
    
    # Downsampling for speed? 
    # If this is done, also need the bin edges and then need to modify code below accordingly
    #RMSTab=downsampleRMSTab(RMSTab)
    
    # Fill in blocks in map for each RMS value
    outFileName=diagnosticsDir+os.path.sep+"massLimitMap_z%s#%s.fits" % (str(z).replace(".", "p"), tileName)
    if os.path.exists(outFileName) == False:
        massLimMap=np.zeros(RMSMap.shape)
        count=0
        t0=time.time()
        for y0Noise in RMSTab['y0RMS']:
            count=count+1
            #print(("... %d/%d (%.3e) ..." % (count, len(RMSTab), y0Noise)))
            compMz=calcCompleteness(RMSTab[np.where(RMSTab['y0RMS'] == y0Noise)], SNRCut, tileName, mockSurvey, 
                                    scalingRelationDict, tckQFitDict, z = z)
            massLimMap[RMSMap == y0Noise]=mockSurvey.log10M[np.argmin(abs(compMz-0.9))]
        t1=time.time()
        mask=np.not_equal(massLimMap, 0)
        massLimMap[mask]=np.power(10, massLimMap[mask])/1e14
        maps.saveFITS(outFileName, massLimMap, wcs, compressed = True)
        
#------------------------------------------------------------------------------------------------------------
def makeMassLimitVRedshiftPlot(massLimit_90Complete, zRange, outFileName, title = None):
    """Write a plot of 90%-completeness mass limit versus z, adding a spline interpolation.
    
    """
    
    plotSettings.update_rcParams()
    plt.figure(figsize=(9,6.5))
    ax=plt.axes([0.10, 0.11, 0.87, 0.86])
    if title is not None:
        plt.figtext(0.15, 0.2, title, ha="left", va="center")
    tck=interpolate.splrep(zRange, massLimit_90Complete)
    plotRange=np.linspace(0, 2, 100)
    plt.plot(plotRange, interpolate.splev(plotRange, tck), 'k-')
    plt.plot(zRange, massLimit_90Complete, 'D', ms = 8)
    plt.xlabel("$z$")
    plt.ylim(0.5, 8)
    plt.xticks(np.arange(0, 2.2, 0.2))
    plt.xlim(0, 2)
    labelStr="$M_{\\rm 500c}$ (10$^{14}$ M$_{\odot}$) [90% complete]"
    plt.ylabel(labelStr)
    plt.savefig(outFileName)
    if outFileName.find(".pdf") != -1:
        plt.savefig(outFileName.replace(".pdf", ".png"))
    plt.close()   
    
#------------------------------------------------------------------------------------------------------------
def cumulativeAreaMassLimitPlot(z, diagnosticsDir, selFnDir, tileNames):
    """Make a cumulative plot of 90%-completeness mass limit versus survey area, at the given redshift.
    
    """
    
    # NOTE: We can avoid this if we add 'areaDeg2' column to selFn_mapFitTab_*.fits tables
    allLimits=[]
    allAreas=[]
    for tileName in tileNames:
        massLimMap, wcs=loadMassLimitMap(tileName, diagnosticsDir, z)
        areaMap, wcs=loadAreaMask(tileName, selFnDir)
        areaMapSqDeg=(maps.getPixelAreaArcmin2Map(areaMap, wcs)*areaMap)/(60**2)
        limits=np.unique(massLimMap).tolist()
        if limits[0] == 0:
            limits=limits[1:]
        areas=[]
        for l in limits:
            areas.append(areaMapSqDeg[np.where(massLimMap == l)].sum())
        allLimits=allLimits+limits
        allAreas=allAreas+areas
    
    # Reduce redundant mass limits
    allLimits=np.array(allLimits)
    allAreas=np.array(allAreas)
    uniqLimits=np.unique(allLimits)
    uniqAreas=[]
    for u in uniqLimits:
        uniqAreas.append(allAreas[allLimits == u].sum())
    uniqAreas=np.array(uniqAreas)
    tab=atpy.Table()
    tab.add_column(atpy.Column(uniqLimits, 'MLim'))
    tab.add_column(atpy.Column(uniqAreas, 'areaDeg2'))
    tab.sort('MLim')
    
    plotSettings.update_rcParams()
        
    # Full survey plot
    plt.figure(figsize=(9,6.5))
    ax=plt.axes([0.155, 0.12, 0.82, 0.86])
    plt.minorticks_on()
    plt.plot(tab['MLim'], np.cumsum(tab['areaDeg2']), 'k-')
    plt.ylabel("survey area < $M_{\\rm 500c}$ limit (deg$^2$)")
    plt.xlabel("$M_{\\rm 500c}$ (10$^{14}$ M$_{\odot}$) [90% complete]")
    labelStr="total survey area = %.0f deg$^2$" % (np.cumsum(tab['areaDeg2']).max())
    plt.ylim(0.0, 1.2*np.cumsum(tab['areaDeg2']).max())
    plt.xlim(0,)
    plt.figtext(0.2, 0.9, labelStr, ha="left", va="center")
    plt.savefig(diagnosticsDir+os.path.sep+"cumulativeArea_massLimit_z%s.pdf" % (str(z).replace(".", "p")))
    plt.savefig(diagnosticsDir+os.path.sep+"cumulativeArea_massLimit_z%s.png" % (str(z).replace(".", "p")))
    plt.close()
    
    # Deepest 20% - we show a bit beyond this
    totalAreaDeg2=tab['areaDeg2'].sum()
    deepTab=tab[np.where(np.cumsum(tab['areaDeg2']) < 0.25 * totalAreaDeg2)]
    plt.figure(figsize=(9,6.5))
    ax=plt.axes([0.155, 0.12, 0.82, 0.86])
    plt.minorticks_on()
    plt.plot(tab['MLim'], np.cumsum(tab['areaDeg2']), 'k-')
    plt.ylabel("survey area < $M_{\\rm 500c}$ limit (deg$^2$)")
    plt.xlabel("$M_{\\rm 500c}$ (10$^{14}$ M$_{\odot}$) [90% complete]")
    labelStr="area of deepest 20%% = %.0f deg$^2$" % (0.2 * totalAreaDeg2)
    plt.ylim(0.0, 1.2*np.cumsum(deepTab['areaDeg2']).max())
    plt.xlim(0,)
    plt.figtext(0.2, 0.9, labelStr, ha="left", va="center")
    plt.savefig(diagnosticsDir+os.path.sep+"cumulativeArea_massLimit_z%s_deepest20Percent.pdf" % (str(z).replace(".", "p")))
    plt.savefig(diagnosticsDir+os.path.sep+"cumulativeArea_massLimit_z%s_deepest20Percent.png" % (str(z).replace(".", "p")))
    plt.close()
        
#------------------------------------------------------------------------------------------------------------
def makeFullSurveyMassLimitMapPlot(z, config):
    """Reprojects tile mass limit maps onto the full map pixelisation, then makes a plot and saves a
    .fits image.
        
    """
    
    if 'makeQuickLookMaps' not in config.parDict.keys():
        config.quicklookScale=0.25
        config.quicklookShape, config.quicklookWCS=maps.shrinkWCS(config.origShape, config.origWCS, config.quicklookScale)

    outFileName=config.diagnosticsDir+os.path.sep+"reproj_massLimitMap_z%s.fits" % (str(z).replace(".", "p"))
    maps.stitchTiles(config.diagnosticsDir+os.path.sep+"massLimitMap_z%s#*.fits" % (str(z).replace(".", "p")), 
                     outFileName, config.quicklookWCS, config.quicklookShape, 
                     fluxRescale = config.quicklookScale)

    # Make plot
    if os.path.exists(outFileName) == True:
        with pyfits.open(outFileName) as img:
            for hdu in img:
                if hdu.shape is not ():
                    reproj=np.nan_to_num(hdu.data)
                    reproj=np.ma.masked_where(reproj <1e-6, reproj)
                    wcs=astWCS.WCS(hdu.header, mode = 'pyfits')
            plotSettings.update_rcParams()
            fontSize=20.0
            figSize=(16, 5.7)
            axesLabels="sexagesimal"
            axes=[0.08,0.15,0.91,0.88]
            cutLevels=[2, int(np.median(reproj[np.nonzero(reproj)]))+2]
            colorMapName=colorcet.m_rainbow
            fig=plt.figure(figsize = figSize)
            p=astPlots.ImagePlot(reproj, wcs, cutLevels = cutLevels, title = None, axes = axes, 
                                axesLabels = axesLabels, colorMapName = colorMapName, axesFontFamily = 'sans-serif', 
                                RATickSteps = {'deg': 30.0, 'unit': 'h'}, decTickSteps = {'deg': 20.0, 'unit': 'd'},
                                axesFontSize = fontSize)
            cbLabel="$M_{\\rm 500c}$ (10$^{14}$ M$_{\odot}$) [90% complete]"
            cbShrink=0.7
            cbAspect=40
            cb=plt.colorbar(p.axes.images[0], ax = p.axes, orientation="horizontal", fraction = 0.05, pad = 0.18, 
                            shrink = cbShrink, aspect = cbAspect)
            plt.figtext(0.53, 0.04, cbLabel, size = 20, ha="center", va="center", fontsize = fontSize, family = "sans-serif")
            plt.savefig(outFileName.replace(".fits", ".pdf"), dpi = 300)
            plt.savefig(outFileName.replace(".fits", ".png"), dpi = 300)
            plt.close()

#------------------------------------------------------------------------------------------------------------
def tidyUp(config):
    """Tidy up the selFn directory - making MEF files etc. and deleting individual tile files.
    
    """

    # Make MEFs
    MEFsToBuild=["areaMask", "RMSMap_%s" % (config.parDict['photFilter'])]
    if 'selFnFootprints' in config.parDict.keys():
        for footprintDict in config.parDict['selFnFootprints']:
            MEFsToBuild.append("intersect_%s" % footprintDict['label'])
    for MEFBaseName in MEFsToBuild:  
        outFileName=config.selFnDir+os.path.sep+MEFBaseName+".fits"
        newImg=pyfits.HDUList()
        filesToRemove=[]
        for tileName in config.allTileNames:
            fileName=config.selFnDir+os.path.sep+MEFBaseName+"#"+tileName+".fits"
            if os.path.exists(fileName):
                with pyfits.open(fileName) as img:
                    # Handling compressed images when we're not actually using tiles
                    if img[tileName].data is None and tileName == 'PRIMARY':
                        tileName='COMPRESSED_IMAGE'
                    hdu=pyfits.CompImageHDU(np.array(img[tileName].data, dtype = float), img[tileName].header, name = tileName)
                filesToRemove.append(fileName)
                newImg.append(hdu)
        if len(newImg) > 0:
            try:
                newImg.writeto(outFileName, overwrite = True)
            except:
                print("argh")
                IPython.embed()
                sys.exit()
            for f in filesToRemove:
                os.remove(f)
            
    # Combine RMSTab files (we can downsample further later if needed)
    # We add a column for the tileName just in case want to select on this later
    footprints=['']
    if 'selFnFootprints' in config.parDict.keys():
        for footprintDict in config.parDict['selFnFootprints']:
            footprints.append(footprintDict['label'])
    strLen=0
    for tileName in config.allTileNames:
        if len(tileName) > strLen:
            strLen=len(tileName)
    for footprint in footprints:
        if footprint != "":
            label="_"+footprint
        else:
            label=""
        outFileName=config.selFnDir+os.path.sep+"RMSTab"+label+".fits"
        tabList=[]
        filesToRemove=[]
        for tileName in config.allTileNames:
            fileName=config.selFnDir+os.path.sep+"RMSTab_"+tileName+label+".fits"
            if os.path.exists(fileName):
                tileTab=atpy.Table().read(fileName)
                tileTab.add_column(atpy.Column(np.array([tileName]*len(tileTab), dtype = '<U%d' % (strLen)), 
                                               "tileName"))
                tabList.append(tileTab)
                filesToRemove.append(fileName)
        if len(tabList) > 0:
            tab=atpy.vstack(tabList)
            tab.sort('y0RMS')
            tab.write(outFileName, overwrite = True)
            for f in filesToRemove:
                os.remove(f)

        
    
