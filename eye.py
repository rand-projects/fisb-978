#!/usr/bin/env python3

"""
eye.py - Display Eye Diagrams of captured Data
==============================================

Uses demodulated data captured from the ec_978.py '--saveraw' command to display
eye diagrams.

Note: In the code, you will often see that we drop the first sample.
This is because ec_978.py supplies an extra sample at the beginning. The first
data point is actually packet[1].

Data is displayed in smoothed or non-smoothed mode. Non-smoothed is just
the raw data samples. Smoothed is the same data interpolated at eight times
the original data. Some diagrams will also use an alpha (transparency) value
for display.
"""
import sys
import os
import numpy as np
import argparse
from argparse import RawTextHelpFormatter
import matplotlib.pyplot as plt
import matplotlib
from commpy.filters import rrcosfilter
from commpy.filters import rcosfilter
from scipy import signal
from colour import Color

from ec_978 import PACKET_LENGTH_FISB, PACKET_LENGTH_ADSB

# number of points to show in x-axis (3 = 0,1,2,3)
# The number of samples for each interval is (points * 2) + 1
nPoints = 3

def plotSingleEye(packet, n, nPoints, ax, xAxis, clr, scale):
  """
  Plot a single single set of data points.

  Args:
    packet (np array): Complete set of demodulated data.
    n (int): Which set of points to plot. 0 is the first set,
      1 is the second, etc.
    nPoints (int): Number of complete set of data points to plot,
      usually set by the global 'nPoints'.
    ax (Axes): matplotlib Axes object.
    xAxis (array): Point values for x-axis as array.
    clr (str): Color. None implies each plot will use a different
      color.
    scale (int): 1 if plotting data points with no interpolation,
      or 8 if using 8 point interpolation.
  """
  nPointsSampleNum = ((nPoints * 2) * scale) + scale

  start = n * (nPointsSampleNum - scale)
  finish = start + nPointsSampleNum

  ax.plot(xAxis, packet[start:finish], c=clr)

def totalAvailableSamples(packet, nPoints, scale):
  """
  Compute the total number of available plots available for a
  given packet as well as the number of data points in a single
  plot.

  Args:
    packet (np array): Complete set of demodulated data.
    nPoints (int): Number of complete set of data points to plot,
      usually set by the global 'nPoints'.
    scale (int): 1 if plotting data points with no interpolation,
      or 8 if using 8 point interpolation.

  Returns:
    tuple: Tuple containing:

    * Number of points for each sample (int).
    * Total number of plots that can be plotted for this packet (int)
  """
  nPointsSampleNum = ((nPoints * 2) * scale) + scale
  packetSize = packet.size
  
  return nPointsSampleNum, int(packet.size / nPointsSampleNum)

def eyeDiagramEarlyAndLate(packet):
  """
  Plot eye diagram with 1st 10% and last 10% of samples.
  Note that for ADS-B short packets, the last 10% is probably
  just baseline noise.

  No smoothing.

  The 1st 10% of the samples will be gray and the last 10% will be tan.
  The reason for the color difference is to test for shifting which might
  indicate a wrong sampling rate (or the SDR card sampling rate is off
  from what it should be).

  Args:
    packet (np array): Complete set of demodulated data.
  """

  # Drop the first sample
  packet = packet[1:]

  nPointsSampleNum, maxSamples = totalAvailableSamples(packet, nPoints, 1)

  sampleSizeInPercent = int(maxSamples * 0.10)

  xAxis = np.linspace(0, nPoints, nPointsSampleNum)
  fig, ax = plt.subplots(figsize=(6, 7.5))
  plt.axhline(y=0, c='red', linestyle='--')
  plt.xlabel('Data points (2 samples / data point)')
  plt.ylabel('Values')
  plt.title('First 10% gray, last 10% tan')
  
  for x in range(0, sampleSizeInPercent):
    # Grey (alpha .5)
    plotSingleEye(packet, x, nPoints, ax, xAxis, (.57, .58, .57, .5), 1)

  for x in range(maxSamples - sampleSizeInPercent, maxSamples):
    # Tan (alpha .5)
    plotSingleEye(packet, x, nPoints, ax, xAxis, (.81, .69, .43, .5), 1)

  plt.show()

def eyeDiagramEarlyAndLateSmooth(packet):
  """
  Plot eye diagram with 1st 10% and last 10% of samples.
  Note that for ADS-B short packets, the last 10% is probably
  just baseline noise.

  8x smoothing is applied.

  Each plot will get a different color.

  Args:
    packet (np array): Complete set of demodulated data.
  """
  # Drop the first sample
  packet = packet[1:]
  
  packet1 = signal.resample(packet, packet.size * 8)
  
  nPointsSampleNum, maxSamples = totalAvailableSamples(packet1, nPoints, 8)

  sampleSizeInPercent = int(maxSamples * 0.10)

  xAxis = np.linspace(0, nPoints, nPointsSampleNum)
  fig, ax = plt.subplots(figsize=(6, 7.5))
  plt.axhline(y=0, c='black', linestyle='--')
  plt.xlabel('Data points (2 samples / data point)')
  plt.ylabel('Values')
  plt.title('First 10%, last 10% data points (w/smoothing)')

  for x in range(0, sampleSizeInPercent):
    plotSingleEye(packet1, x, nPoints, ax, xAxis, None, 8)
  for x in range(maxSamples - sampleSizeInPercent, maxSamples):
    plotSingleEye(packet1, x, nPoints, ax, xAxis, None, 8)

  plt.show()

def eyeDiagramSmooth(packet):
  """
  Plot eye diagram using all samples.

  8x smoothing is applied.

  Args:
    packet (np array): Complete set of demodulated data.
  """
  # Drop the first sample
  packet = packet[1:]

  packet1 = signal.resample(packet, packet.size * 8)

  nPointsSampleNum, maxSamples = totalAvailableSamples(packet1, nPoints, 8)

  xAxis = np.linspace(0, nPoints, nPointsSampleNum)
  fig, ax = plt.subplots(figsize=(6, 7.5))
  plt.axhline(y=0, c='black', linestyle='--')
  plt.xlabel('Data points (2 samples / data point)')
  plt.ylabel('Values')
  plt.title('All data points eye diagram (with smoothing)')

  for x in range(0, maxSamples):
    plotSingleEye(packet1, x, nPoints, ax, xAxis, None, 8)

  plt.show()

def eyeDiagramSmoothGradient(packet, isForward):
  """
  Plot eye diagram using all samples. The colors are a gradient starting
  with red, then going to yellow, green and blue. The idea is that you
  can look at outliers and see where in the packet they are located.
  
  8x smoothing is applied.

  Args:
    packet (np array): Complete set of demodulated data.
    isForward (bool): True if displaying the packet from front to
      back. False if displaying packet from back to front. Red is
      always used for the front packets, and blue for the rear 
      packets.
  """
  # Drop the first sample
  packet = packet[1:]

  packet1 = signal.resample(packet, packet.size * 8)

  nPointsSampleNum, maxSamples = totalAvailableSamples(packet1, nPoints, 8)

  if isForward:
    start = 0
    stop = maxSamples
    interval = 1
    pltTitle = 'All points, smooth, gradient front to back'
  else:
    start = maxSamples - 1
    stop = -1
    interval = -1
    pltTitle = 'All points, smooth, gradient back to front'

  red = Color('red')
  colors = list(red.range_to(Color('blue'), maxSamples))
  
  xAxis = np.linspace(0, nPoints, nPointsSampleNum)
  fig, ax = plt.subplots(figsize=(6, 7.5))
  plt.axhline(y=0, c='black', linestyle='--')
  plt.xlabel('Data points (2 samples / data point)')
  plt.ylabel('Values')
  plt.title(pltTitle)

  if isForward:
    start = 0
    stop = maxSamples
    interval = 1
  else:
    start = maxSamples - 1
    stop = -1
    interval = -1

  for x in range(start, stop, interval):
    plotSingleEye(packet1, x, nPoints, ax, xAxis, \
        matplotlib.colors.to_rgba(colors[x].rgb, .1), 8)

  plt.show()

def eyeDiagram(packet):
  """
  Plot eye diagram using all samples.

  No smoothing.

  Args:
    packet (np array): Complete set of demodulated data.
  """
  # Drop the first sample
  packet = packet[1:]

  nPointsSampleNum, maxSamples = totalAvailableSamples(packet, nPoints, 1)

  xAxis = np.linspace(0, nPoints, nPointsSampleNum)
  fig, ax = plt.subplots(figsize=(6, 7.5))
  plt.axhline(y=0, c='black', linestyle='--')
  plt.xlabel('Data points (2 samples / data point)')
  plt.ylabel('Values')
  plt.title('All data points eye diagram (no smoothing)')

  for x in range(0, maxSamples):
    plotSingleEye(packet, x, nPoints, ax, xAxis, None, 1)

  plt.show()

def main(fname):
  """
  Read 'fname' from disk and display requested images.

  Args:
    fname (str): Filename containing data to process.
  """
  # See if file is FIS-B or ADS-B
  if '.F.' in fname:
    isFisb = True
    packetLength = PACKET_LENGTH_FISB
  elif '.A' in fname:
    isFisb = False
    packetLength = PACKET_LENGTH_ADSB
  else:
    print('Cannot tell if file is FIS-B or ADS-B.')
    sys.exit(1)

  with open(fname, 'rb') as bfile:
    packetBuf = bfile.read(packetLength)

  # Numpy will convert the bytes to int32's
  packet = np.frombuffer(packetBuf, np.int32)

  if showAdpns:
    eyeDiagram(packet)
  if showAdps:
    eyeDiagramSmooth(packet)
  if showAdpgfb:
    eyeDiagramSmoothGradient(packet, True)
  if showAdpgbf:
    eyeDiagramSmoothGradient(packet, False)
  if showFlns:
    eyeDiagramEarlyAndLate(packet)
  if showFls:
    eyeDiagramEarlyAndLateSmooth(packet)  
  
# Call main function
if __name__ == "__main__":

  hlpText = \
  """eye.py: Show signal as eye diagram.

Uses the data from 'ec_978.py' with the '--saveraw' flag set to show an
eye diagram.

Not specifying any optional arguments implies all eye diagrams will be
displayed in sequence.

Each diagram shows approximately 3 data points (6 + 1 samples).
Data is sampled at 2.083334 Mhz, or twice the data rate of 1.041667 Mhz.
Non-smoothed eyes shows only the actual samples. Smoothed samples are
up-sampled and interpolated by a factor of eight.

Either FIS-B or ADSB data can be displayed. ADS-B is tuned for long
ADS-B packets (the most common). If, when looking at an ADS-B eye
diagram, you see a thin horizontal line, this denotes a short packet.
The horizontal line represents no data transmission which is usually
at a very low magnitude compared with the signal. First and last 10%
diagrams will only show the first 10%, because the last 10% is not
modulated.

Some eye diagrams only show the first 10% of data, and the last 10%
of data. For the non-smoothed version, the first 10% is shown in gray,
and the last 10% in tan. This way you can detect an abnormal sampling
frequency. The two colors should mostly overlap.

The smoothed first and last 10% uses a normal color scheme. A full
FIS-B packet can clutter up the display, and only showing 20% of
samples is easier to interpret. Also, FIS-B packets have ones and 
zeroes at both ends, while placeholder packets have only zeros in
the middle.

Data points can be shown with 'gradient colors'. Early data will
start off in red, then gradually turn to yellow, green, and finally
blue. You can use gradient colors to  observe outlier values
and tell where within the packet they occurred. '--adpgfb' displays
the data front to back (red to blue), and '--adpgbf' will display
the data back to front (blue to red). Red will always be the early
data and blue the last data. Gradient colors also help visualize the
eye better in noisy signals.

If you are displaying more than one diagram, the next diagram will 
appear after closing the window of the current diagram.
"""
  parser = argparse.ArgumentParser(description= hlpText, \
          formatter_class=RawTextHelpFormatter)
          
  parser.add_argument("fname", help='Filename to use.')

  parser.add_argument("--all", \
    help='Display all graphs in sequence.', action='store_true')
  parser.add_argument("--adpns", \
    help='Show all data points no smoothing.', action='store_true')
  parser.add_argument("--adps", \
    help='Show all data points with smoothing.', action='store_true')
  parser.add_argument("--adpgfb", \
    help='Show all data points gradient colors front to back.', action='store_true')
  parser.add_argument("--adpgbf", \
    help='Show all data points gradient colors back to front.', action='store_true')
  parser.add_argument("--flns", \
    help='Show first and last 10 pct no smoothing.', action='store_true')
  parser.add_argument("--fls", \
    help='Show first and last 10 pct with smoothing.', action='store_true')


  args = parser.parse_args()

  showAdpns = False
  showAdps = False
  showAdpgfb = False
  showAdpgbf = False
  showFlns = False
  showFls = False
  showAll = False

  if args.all:
    showAll = True

  if args.adpns:
    showAdpns = True

  if args.adps:
    showAdps = True
  
  if args.adpgfb:
    showAdpgfb = True
  
  if args.adpgbf:
    showAdpgbf = True
  
  if args.flns:
    showFlns = True

  if args.fls:
    showFls = True

  # Assume --all if no options set, or --all set
  if ((not showAdpns) and (not showAdps) and (not showFlns) and (not showFls) \
      and (not showAll) and (not showAdpgfb) and (not showAdpgbf)) or showAll:
    showAdpns = True
    showAdps = True
    showAdpgfb = True
    showAdpgbf = True
    showFlns = True
    showFls = True

  main(args.fname)
  