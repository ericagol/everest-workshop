#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
tools.py
--------

'''

from __future__ import division, print_function, absolute_import, unicode_literals
import everest
from everest.transit import TransitShape, TransitModel
from everest.math import SavGol
from everest.gp import GetCovariance
import numpy as np
import matplotlib.pyplot as pl
from matplotlib.widgets import Slider, Button
from scipy.linalg import cho_solve, cho_factor
import os
try:
  from tqdm import tqdm
except:
  tqdm = lambda x: x
import logging
log = logging.getLogger(__name__)

def Heatmap(time, delta_chisq, periods, phases):
  '''
  
  '''
  
  dt = np.nanmedian(np.diff(time))
  z = np.zeros((len(periods), len(phases)))
  for i, period in tqdm(enumerate(periods), total = len(periods)):
    for j, phase in enumerate(phases):
      t0 = time[0] + phase * period
      tf = t0 + period * int((time[-1] - t0) / period)
      times = np.arange(t0, tf, period)
      for t in times:
        # NOTE: If your time array isn't uniform, this is
        # better, but way slower: ind = np.argmin(np.abs(time - t))
        ind = int(round((t - time[0]) / dt))
        z[i,j] += delta_chisq[ind]
  return z

def MaskOutliers(star, pos_tol = 2.5, neg_tol = 50.):
  '''
  Keyword `pos_tol` is the positive (i.e., above the median) outlier tolerance in standard deviations.
  Keyword `neg_tol` is the negative (i.e., below the median) outlier tolerance in standard deviations.

  '''
  
  # Smooth the light curve
  t = np.delete(star.time, np.concatenate([star.nanmask, star.badmask]))
  f = np.delete(star.flux, np.concatenate([star.nanmask, star.badmask]))
  f = SavGol(f)
  med = np.nanmedian(f)
  
  # Kill positive outliers
  MAD = 1.4826 * np.nanmedian(np.abs(f - med))
  pos_inds = np.where((f > med + pos_tol * MAD))[0]
  pos_inds = np.array([np.argmax(star.time == t[i]) for i in pos_inds])
  
  # Kill negative outliers
  MAD = 1.4826 * np.nanmedian(np.abs(f - med))
  neg_inds = np.where((f < med - neg_tol * MAD))[0]
  neg_inds = np.array([np.argmax(star.time == t[i]) for i in neg_inds])
  
  # Replace the star.outmask array and make sure we're not masking
  # any transits
  star.outmask = np.concatenate([neg_inds, pos_inds])
  star.transitmask = np.array([], dtype = int)

def GetChunkData(star, chunk, joint_fit = False):
  '''
  
  '''
  
  # These are the unmasked indices for the current chunk
  m = star.get_masked_chunk(chunk, pad = False)
  
  # Get the covariance matrix for this chunk
  K = GetCovariance(star.kernel, star.kernel_params, star.time[m], star.fraw_err[m])
  
  # Are we marginalizing over the systematics model?
  # If so, include the PLD covariance in K and do the
  # search on the *raw* light curve.
  if joint_fit:
    A = np.zeros((len(m), len(m)))
    for n in range(star.pld_order):
      XM = star.X(n,m)
      A += star.lam[chunk][n] * np.dot(XM, XM.T)
    K += A
    flux = star.fraw[m]
  else:
    flux = star.flux[m]
  
  # Compute the Cholesky factorization of K
  C = cho_factor(K)
  
  # Create a uniform time array and get indices of missing cadences
  dt = np.median(np.diff(star.time[m]))
  tol = np.nanmedian(np.diff(star.time[m])) / 5.
  tunif = np.arange(star.time[m][0], star.time[m][-1] + tol, dt)
  time = np.array(tunif)
  gaps = []
  j = 0
  for i, t in enumerate(tunif):
    if np.abs(star.time[m][j] - t) < tol:
      time[i] = star.time[m][j]
      j += 1
      if j == len(star.time[m]):
        break 
    else:
      gaps.append(i)
  gaps = np.array(gaps, dtype = int)
  
  return time, gaps, flux, C

def Search(star, joint_fit = False, clobber = False, **kwargs):
  '''
    
  '''
  
  # Do we need to re-compute everything?
  if clobber or not os.path.exists('target%02d.search%d.npz' % (star.ID, int(joint_fit))):
  
    # Mask bad outliers
    MaskOutliers(star, **kwargs)
  
    # The output arrays
    time = np.array([])
    ml_depth = np.array([])
    depth_variance = np.array([])
    delta_chisq = np.array([])
  
    # Loop over each of the light curve chunks
    for b, brkpt in enumerate(star.breakpoints):
    
      # Log
      log.info('Searching chunk %d/%d...' % (b + 1, len(star.breakpoints)))
    
      # Get the time, gaps, and flux arrays and the Cholesky factorization of the covariance
      t, gaps, flux, C = GetChunkData(star, b, joint_fit = joint_fit)
    
      # The baseline flux level
      baseline = np.nanmedian(flux)
     
      # The likelihood of the data w/ no transit model
      lnL0 = -0.5 * np.dot(flux, cho_solve(C, flux))
    
      # Compute the normalized single-transit model
      transit_model = TransitShape()

      # Initialize the depth, depth variance, and delta chisq for this chunk
      d = np.zeros_like(t)
      var = np.zeros_like(t)
      dchisq = np.zeros_like(t)
    
      # Now roll the transit model across each cadence
      for i in tqdm(range(len(t))):
    
        # Evaluate the transit model centered on this cadence
        trn = transit_model(t, t[i])
        trn = np.delete(trn, gaps)
        trn *= baseline
      
        # The variance of the transit depth estimate for this cadence
        var[i] = 1. / np.dot(trn, cho_solve(C, trn))
      
        # Infinite variance is bad...
        if not np.isfinite(var[i]):
          var[i] = np.nan
          d[i] = np.nan
          dchisq[i] = np.nan
          continue
      
        # This is the highest likelihood depth
        d[i] = var[i] * np.dot(trn, cho_solve(C, flux))
      
        # The residual vector: the flux minus the transit model
        r = flux - trn * d[i]
      
        # The log-likelihood of the transit model
        lnL = -0.5 * np.dot(r, cho_solve(C, r))
      
        # Compute the delta-chi squared compared to no transit model
        dchisq[i] = -2 * (lnL0 - lnL)
    
      # Add the results for this chunk to the running lists
      time = np.append(time, t)
      ml_depth = np.append(ml_depth, d)
      depth_variance = np.append(depth_variance, var)
      delta_chisq = np.append(delta_chisq, dchisq)

    # Save!
    np.savez('target%02d.search%d.npz' % (star.ID, int(joint_fit)), time = time, ml_depth = ml_depth, depth_variance = depth_variance, delta_chisq = delta_chisq)
  
  else:
  
    # Load from disk
    data = np.load('target%02d.search%d.npz' % (star.ID, int(joint_fit)))
    time = data['time']
    ml_depth = data['ml_depth']
    depth_variance = data['depth_variance']
    delta_chisq = data['delta_chisq']
  
  return time, ml_depth, depth_variance, delta_chisq
  
class Load(everest.Everest):
  '''
  An emulator for the Everest class.
  
  '''
  
  def __init__(self, ID, quiet = False, clobber = False, **kwargs):
    '''
    
    '''
    
    # Read kwargs
    self.ID = ID
    self._season = 5
    self.mission = 'k2'
    self.clobber = clobber
    self.cadence = 'lc'
    
    # Initialize preliminary logging
    if not quiet:
      screen_level = logging.DEBUG
    else:
      screen_level = logging.CRITICAL
    everest.utils.InitLog(None, logging.DEBUG, screen_level, False)

    # Load
    self.fitsfile = 'target%02d.fits' % self.ID
    self.model_name = 'rPLD'
    self._weights = None
    self.load_fits()

  @property
  def dir(self):
    '''
    Returns the directory where the raw data and output for the target is stored.
    
    '''
    
    return os.path.dirname(os.path.abspath(__file__))
  
  def search(self, joint_fit = False, clobber = False, periods = np.linspace(5., 20., 1000), phases = np.linspace(0., 1., 1000), **kwargs):
    '''
    
    '''
    
    # Call the Search function
    time, ml_depth, depth_variance, delta_chisq = Search(self, joint_fit = joint_fit, clobber = clobber, **kwargs)
    
    # Plot the delta chisq timeseries
    fig, ax = pl.subplots(3, figsize = (8, 7), sharex = True)
    fig.subplots_adjust(hspace = 0.05, bottom = 0.15)
    ax[0].plot(self.time, self.flux / np.nanmedian(self.flux), 'k.', alpha = 0.3, ms = 2)
    ax[1].plot(time, delta_chisq, lw = 1, color = 'k', alpha = 0.65)
    ax[1].set_ylim(0, 1.1 * np.nanmax(delta_chisq))
    pl_cond, = ax[2].plot(time, delta_chisq, lw = 1, color = 'k', alpha = 0.65)

    # Labels and stuff
    ax[0].set_ylabel(r'Flux', fontsize = 18)
    ax[1].set_ylabel(r'$\Delta \chi^2$', fontsize = 18)
    ax[2].set_ylabel(r'$\Delta \chi^2_\mathrm{cond}$', fontsize = 18)
    ax[2].set_xlabel("Time [days]", fontsize = 18)
    ax[0].yaxis.set_label_coords(-0.075,0.5)
    ax[1].yaxis.set_label_coords(-0.075,0.5)
    ax[2].yaxis.set_label_coords(-0.075,0.5)
    if joint_fit:
      ax[0].set_title('Joint instrumental/transit model', fontweight = 'bold')
    else:
      ax[0].set_title('De-trend then search', fontweight = 'bold')

    # Allow the user to change the `true` depth
    axslider = pl.axes([0.125, 0.025, 0.65, 0.03])
    slider = Slider(axslider, r'$Depth$', -6, -2, valinit = -6)
    slider.valtext.set_x(0.5)
    slider.valtext.set_ha('center')
    def update(val):
      d = 10 ** slider.val
      slider.valtext.set_text("%.3e" % d)
      delta_chisq_cond = delta_chisq - (ml_depth - d) ** 2 / depth_variance
      pl_cond.set_ydata(delta_chisq_cond)
      ax[2].set_ylim(0, max(10, 1.1 * np.nanmax(delta_chisq_cond)))
      fig.canvas.draw()
      return pl_cond, 
    slider.on_changed(update)
    update(-6)
    
    # Allow the user to plot the heat map
    axbutton = pl.axes([0.8, 0.025, 0.1, 0.03])
    button = Button(axbutton, 'Heatmap!')
    callback = lambda x: self.heatmap(delta_chisq - (ml_depth - 10 ** slider.val) ** 2 / depth_variance, periods = periods, phases = phases)
    button.on_clicked(callback)
    
    # Show and return the results
    pl.show()
    return time, ml_depth, depth_variance, delta_chisq
  
  def heatmap(self, delta_chisq, periods = np.linspace(5., 20., 1000), phases = np.linspace(0., 1., 1000), **kwargs):
    '''
    
    '''
    
    # Call the Heatmap function
    log.info('Computing delta chisq heatmap...')
    z = Heatmap(self.time, delta_chisq, periods, phases)
    
    # Plot
    fig, ax = pl.subplots(1)
    im = ax.imshow(z.T, origin = 'lower', extent = (periods[0], periods[-1], phases[0], phases[-1]), aspect = 'auto', vmin = 0, vmax = np.nanmax(z), cmap = pl.get_cmap('plasma'))
    pl.colorbar(im, label = r'$\Sigma\Delta \chi^2$')
    ax.set_xlabel('Period [days]', fontweight = 'bold')
    ax.set_ylabel('Phase', fontweight = 'bold')
    
    # Plot the highest likelihood model
    i, j = np.unravel_index(np.nanargmax(z), z.shape)
    pl.plot(periods[i], phases[j], 'o', ms = 20, markerfacecolor = 'none', markeredgecolor = 'r')
    
    # Show and return the heatmap
    pl.show()
    return z
    