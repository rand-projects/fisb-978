#!/usr/bin/env python3

"""
ec_978 - Error Correct FIS-B and ADS-B messages.
================================================

Accepts packets (either FIS-B or ADS-B), usually sent from
``demod-978``, into standard input. 

The input is sent as two messages, the first is a 30 byte character
string which provides information about the data packet to follow.
This is then followed by a datapacket as bytes suitable as a numpy
I32 array. Two sizes of packets are sent (and indicated as such in the 
preceding 30 byte attribute packet): ``PACKET_LENGTH_FISB`` (35340)
for FIS-B packets and ``PACKET_LENGTH_ADSB`` (3084) for ADS-B packets.

Note that while there are two types of ADS-B packets, short and long,
the length is set for long packets. That allows for long and short packets
to be error corrected (you can sort of, but not absolutely, distinguish
between the two at the time they are sent).

After the packets are received, they are packed together into bytes 
and error corrected using the Reed-Solomon parity bits. If the initial error
correction is not successful, the bits before and after each bit are used
in a set of weighted averages to come up with a new set of bits. These bits
are then tried. The concept is that since we are sampling at the
Nyquist frequency, the bits actually sampled are usually not at optimum
sampling points. By using their 'bit buddies' (neighbor bits) we can
come up with more optimum sampling points. Empirically, this is
very effective.

If FIS-B packets are not correctly decoded using the above method, other methods
such as supplying known fixed bits in block 0, or detecting runs of zeroes
at the ends of blocks are tried.

Successful corrections are passed to standard output.
"""

import sys
import os
import os.path
import numpy as np
import pyreedsolomon as rs
import argparse
import glob
import time
from datetime import timezone, datetime, timedelta
import shutil
from argparse import RawTextHelpFormatter

# List of positions to shift bits by to error correct messages.
#
# Positive values means you use the bits before, negative values means you use
# the bits after. These are the most likely values to result in a match
# ordered by the likeliness (empirically determined).
# 
# This list was created a large number of samples. We found the amount that
# would error correct the largest percentage of packets, and then removed those
# packets from contention. The process was then repeated. It was also found
# that a granularity of less than 5% wasn't useful.
SHIFT_BY_PROBABILITY = [0, -0.75, 0.75, -0.50, 0.50, -0.25, \
      0.25, -0.85, 0.40, 0.65, -0.30, 0.80, -.05, 0.05, -0.90, 0.90, \
      -0.10, 0.10, 0.85, -0.15, 0.15, -0.80, -0.65, -0.35, 0.35, \
      -0.70, 0.70, 0.30, -0.40, -0.60, 0.60, -0.20, 0.20, -0.45, \
      0.45, -0.55, 0.55]

# Table that maps the uplink feedback code to the number of packets
# received by a particular channel in the last 32 seconds. These are
# the number of FIS-B packets received by an aircraft over a 
# particular channel. The channel is rotated every second.
UPLINK_FEEDBACK_TABLE = [ '0', '1-13', '14-21', '22-25', '26-28', \
    '29-30', '31', '32']

# Maps UAT data channel to the power of the station and TIS-B site id.
FISB_DATA_CHANNEL = [ \
    'H15','H14','H13','M12','M11','M10','L8','L5|S3', \
    'H15','H14','H13','M12','M11','L9','L7','L6|S2', \
    'H15','H14','H13','M12','M10','L9','L7','L6|S1', \
    'H15','H14','H13','M11','M10','L8', 'L6','S4']

# Base 40 table for decoding the call signs of ADS-B packets.
BASE40_TABLE = ['0','1','2','3','4','5','6','7','8','9', \
    'A','B','C','D','E','F','G','H','I','J','K','L','M', \
    'N','O','P','Q','R','S','T','U','V','W','X','Y','Z', \
    ' ','','x','y']

# Number of bits in a raw FIS-B data block (with RS bits)
BITS_PER_BLOCK = 4416

# Number of bytes in a raw FIS-B data block (with RS bits)
BYTES_PER_BLOCK = BITS_PER_BLOCK // 8

#: Length of attribute string sent with each packet.
ATTRIBUTE_LEN = 30

#: Size of a FIS-B packet in bytes.
#: Derived from ((4416 * 2) + 3) * 4.
#: FIS-B packet has 4416 bits * 2 for 2 samples/bit + 3 for extra
#: bit before actual data and 2 after. Multiplied by 4 since each
#: sample is 4 bytes long.
PACKET_LENGTH_FISB = 35340

#: Size of an ADS-B packet in bytes.
#: Derived from ((384 * 2) + 3) * 4.
#: ADS-B long packet has 384 bits * 2 for 2 samples/bit + 3 for extra
#: bit before actual data and 2 after. Multiplied by 4 since each
#: sample is 4 bytes long. We consider all ADS-B packets as long
#: since this will capture all cases (long and short ADS-B).
PACKET_LENGTH_ADSB = 3084

# If True, show information about failed FIS-B packets as a comment.
show_failed_fisb = False

# If True, show information about failed ADS-B packets as a comment.
show_failed_adsb = False

# If True, do an ADS-B partial decode. Turned on by --apd flag.
adsb_partial_decode = False

# Directory and flag for writing error files.
dir_out_errors = None
writingErrorFiles = False

# Flag to show lowest usable signal levels. Set by --ll.
show_lowest_levels = False

# Flag to repair block zero fixed (not changing) bits.
# Set by --bzfb flag.
block_zero_fixed_bits = True

# Repair blocks that end in trailing zeros.
fix_trailing_zeros = True

# Set to True if replacing first 6 byte values. Set by --f6b.
replace_f6b = False

# For --f6b flag, holds number of 1st 6 byte values to check.
f6bArrayLen = 0

# For --f6b flag, contains contents of 1st 6 byte values.
f6bArray = None

# Reed-Solomon error correction Ground Uplink parameters:
#   Symbol Size: 8
#   Message Symbols: 72 (ADS-B short: 144, ADS-B long: 272)
#   Message + ECC symbols: 92 (ADS-B short: 240, ADS-B long: 384)
#   Galois field generator polynomial coefficients: 0x187
#   fcr (First Consecutive Root): 120
#   Primitive element: 1
#   Number of roots: 20 (ADS-B short: 12, ADS-B long: 14)
#    (same as the number of parity bits)
rsAdsbS = rs.Reed_Solomon(8,18,30,0x187,120,1,12)
rsAdsbL = rs.Reed_Solomon(8,34,48,0x187,120,1,14)
rsFisb = rs.Reed_Solomon(8,72,92,0x187,120,1,20)

def block0ThoroughCheck(hexBlocks):
  """
  Look at all consecutive blocks starting with block 0 and see
  if the packet has ended and the rest of the packet is
  zero-filled. If so, ``hexBlocks`` will be altered and all
  remaining blocks will be filled in.

  Args:
    hexBlocks (list): List of 6 hex blocks, each containing a
      string of hex values if already error corrected, or ``None``
      if not error corrected.
      
  Returns:
    tuple: Tuple containing:

    * ``True`` if this packet matched. Else ``False``.
      ``True`` indicates the packet (all 6 blocks) is entirely error corrected.
    * If this was a successful error correction, ``hexBlocks`` is altered
      to contain values for all 6 blocks. Otherwise, the value that
      ``hexBlocks`` contained when the function was called is
      returned.
  """
  
  # If we don't have a block 0, we can't continue
  if hexBlocks[0] == None:
    return False, hexBlocks
  
  # Gather all the consecutive blocks together, starting with block 0
  hexData = hexBlocks[0]
  lastBlock = 0
  for i in range(1,5):
    if hexBlocks[i] != None:
      hexData += hexBlocks[i]
      lastBlock += 1
    else:
      # Stop at the first non-consecutive block
      break

  # Turn hex string back to bytes.
  dataBytes = bytearray.fromhex(hexData)
  dataBytesLen = len(dataBytes)

  # Jump through UAT Frames, looking for an empty frame.
  bytePtr = 8

  # Loop until we have looped over all packets we have.
  while True:
    if bytePtr + 1 >= dataBytesLen:
      break

    uatFrameLen = (dataBytes[bytePtr] << 1) | (dataBytes[bytePtr + 1] >> 7)

    if uatFrameLen == 0:
      # Found end of frame, so we matched.
      currentBlock = (bytePtr + 1) // 72

      # Set all future blocks to zeroes.
      for i in range(currentBlock+1, 6):
        hexBlocks[i] = '00' * 72
      
      return True, hexBlocks

    # Have a next frame, try to proceed to it
    bytePtr += uatFrameLen + 2

  # Didn't find anything
  return False, hexBlocks

def packAndTest(rs, bits, f6b = False):
  """
  Take a set of integers, each integer representing a
  single bit, and turn them into binary and pack them into
  bytes. Then check them with the Reed-Solomon error correction object.

  Args:
    rs (reed-solomon object): Reed-Solomon object to use.
      This object is different for FIS-B, 
      ADS-B short, and ADS-B long.
    bits (nparray): int32 array containing one integer per bit
      for a single FIS-B block or an entire ADS-B message.
    f6b (bool): ``True`` if we are forcing the first 6 bytes
      of FIS-B block 0 to be a predetermined value. Otherwise ``False``.

  Returns:
    tuple: Tuple containing:

    * String of hex if the error correction was successful, else ``None``.
    * Number of errors detected. Will be positive if a success.
      Otherwise, will be a negative number.
  """
  # Turn integers to 1/0 bits and pack in bytes.
  byts = np.packbits(np.where((bits > 0), 1, 0))

  if not f6b:
    # Do Reed-Solomon error correction.
    errCorrected, errs = rs.decode(byts)

    if errs >= 0:
      errCorrectedHex = errCorrected.tobytes().hex()
      return errCorrectedHex, errs      
    else:
      return None, errs
  else:
    # Try forced value for each entry in list.
    for i in range(0, f6bArrayLen):
      byts[0:6] = f6bArray[i][0:6]

      # Do Reed-Solomon error correction.
      errCorrected, errs = rs.decode(byts)
  
      if errs >= 0:
        errCorrectedHex = errCorrected.tobytes().hex()
        return errCorrectedHex, errs
    
    return None, -74

def extractBlockBitsFisb(samples, offset, blockNumber):
  """
  Given a FIS-B block number, extract an integer array of data bits for the
  packet as supplied, as well as arrays for one sample before and 
  one sample after the normal packet. Applies to FIS-B only.

  Args:
    samples (nparray): Demodulated samples (int32).
    offset (int): This is the offset into ``samples`` where we consider.
      the 'starting' bit to be. It is usually one, although if we don't 
      error correct with 1, 2 will be used later. 1 gets the packet that
      we error corrected the sync word to. 2 will get the packet as the
      set of bits following the packet that we got the sync word for.
    blockNumber (int): Block number we are decoding (0-5).

  Returns:
    tuple: Tuple containing:

    * Array of integers (nparray i32) representing the actual error
      corrected packet.
    * Array of integers (nparray i32) containing 1 bit before the first
      returned result.
    * Array of integers (nparray i32) containing 1 bit after the first
      returned result.
  """
  # Arrays where bits will be placed.
  bitsHolder = np.empty((92,8), dtype=np.int32)
  bitsBeforeHolder = np.empty((92, 8), dtype=np.int32)
  bitsAfterHolder = np.empty((92,8), dtype=np.int32)
  
  # Start bit (and 'samples' running pointer)
  samplesPtr = offset + ((8 * blockNumber) * 2)
  
  # Each block contains 92 data words.
  for i in range(0, 92):
    # Extract the bits for a single 8-bit word.
    bitsHolder[i] = samples[samplesPtr: samplesPtr+16: 2]
    bitsBeforeHolder[i] = samples[samplesPtr-1: samplesPtr + 16 - 1 :2]
    bitsAfterHolder[i] = samples[samplesPtr+1: samplesPtr + 16 + 1 :2]

    # Data is interleaved. This will deinterleave the block.
    samplesPtr += 96 # skip to next byte in block (5 * 8 * 2) + 16

  # Array is a list of 92 items with each element containing an array
  # of 8 integers. The reshape calls will turn this into a single list
  # of 92 * 8 integers.
  return np.reshape(bitsHolder, -1), np.reshape(bitsBeforeHolder, -1), \
         np.reshape(bitsAfterHolder, -1)
  
def extractBlockBitsAdsb(samples, offset, isShort):
  """
  Extract an integer array of data bits for the
  packet as supplied, as well as arrays for one sample before, and 
  one sample after the normal packet. Applies to ADS-B only.

  ADS-B packets come in two flavors, short and long. Most are
  long, but short packets have the first 5 bits set to zero. This gives
  a good first indication of which one to try first, but because of errors,
  both will be tried. There is about a 10:1 ratio of long to short packets.

  Args:
    samples (nparray): Demodulated samples (int32).
    offset (int): This is the offset into ``samples`` where we consider
      the 'starting' bit to be. It is usually one, although if we don't 
      error correct with 1, 2 will be used later. 1 gets the packet that we extracted
      the sync word from. 2 will get the packet as the set of bits following
      the packet that we got the sync word for.
    isShort (bool): ``True`` if we are attempting to error correct a short ADS-B
      packet, or ``False`` if this a long packet attempt.

  Returns:
    tuple: Tuple containing:

    * Array of integers (nparray i32) representing the actual error corrected packet.
    * Array of integers (nparray i32) containing 1 bit before the first returned
      result.
    * Array of integers (nparray i32) containing 1 bit after the first returned
      result.
  """
  # Long packet is 48 bytes.
  numBytes = 48

  if isShort:
    # Short packet is 30 bytes.
    numBytes = 30
  
  # Create the samples
  bits = samples[offset:(numBytes * 16):2]
  bitsBefore = samples[offset-1:(numBytes * 16) - 1:2]
  bitsAfter = samples[offset+1:(numBytes * 16) + 1:2]
  
  return bits, bitsBefore, bitsAfter

def shiftBits(bits, neighborBits, shiftAmount):
  """
  Given a set of packet bits, and either bits that
  came before or after the normal packet bits, multiply
  each neighbor bit by a percentage and add it to the
  packet bit.

  This is the magic behind better error correction. Since we are
  sampling at the Nyquist rate, we can either be sampling
  near the optimum sampling points, or the worst sampling
  points. We use neighbor samples to influence where we
  sample and provide alternate set of values to try to
  error correct closer to the optimum sampling point.

  For really strong signals, this doesn't have much effect,
  most packets will error correct well. But for weaker signals,
  this can provide close to a doubling of packets error corrected.

  Shift amounts are found in ``SHIFT_BY_PROBABILITY`` and
  are ordered by what shift values will produce the quickest
  error corrections (using lots of empirical samples). The first shift 
  is always 0, since this is most likely to match. Well over 99%
  of all matches will match within two tries.

  You can also get a good first approximation by determining
  zero-crossing values, but using the shift table is
  essentially just as fast.

  Args:
    bits (nparray): Sample integers array. Int32.
    neighborBits (nparray): Sample integers array of samples either
      before (``bitsBefore``) or after (``bitsAfter``) the sample. Int32.
    shiftAmount (float): Amount to shift bits toward the 
      ``neighborBits`` as a percentage.

  Returns:
    nparray:  ``bits`` array after shifting.
  """
  # Add a percentage of a neighbor bit to the sample bit.
  # Samples are positive and negative numbers, so this
  # will either raise or lower the sample point.
  shiftedBits = (bits + (neighborBits * shiftAmount))/2
  
  # This an alternative formula for shifted bits, works at the
  # same speed and produces near identical results. Not sure
  # which is the absolute best one to use.
  #
  # One test running against 110,000 errors, this formula resulted
  # in 188 more decodes (5772 to 5564). Over about an 11 minute
  # run, this method was only 5 seconds slower.
  #
  # In a regular run of normal packets, this formula gets the
  # same to slightly lesser number of packets.
  #
  #shiftedBits = ((neighborBits - bits) * shiftAmount) + bits

  return shiftedBits

def tryShiftBits(rs, bits, bitsBefore, bitsAfter, tryFirst, \
      f6b = False, block0FixedBits = False):
  """
  Given 3 packets of data (sample, before sample, and
  after sample), error correct the sample using various shifts.

  The bits represent a single block of a FIS-B message,
  or the entire ADS-B message.

  Args:
    rs (reed-solomon object): Reed-Solomon object to use.
      One of three instances are passed dependent on the
      type of packet being error corrected (FIS-B, ADS-B short,
      ADS-B long).
    bits (nparray): Integers representing the current packet.
    bitsBefore (nparray): Integers representing the bits
      before the current packet.
    bitsAfter (nparray): Integers representing the bits
      after the current packet.
    tryFirst (int): Try this shift first. Is ignored if -1.
      This is used for FIS-B packets to set the shift to one
      that worked for a previous shift.
    f6b (bool): ``True`` if we are to replace the first 6 bytes of a block
      0 packet with a set of fixed values. If this is ``True``, it is assumed that
      ``bits`` is from a block 0 packet.
    block0FixedBits (bool): ``True`` if we are to force a set of constant bits
      for block 0. If ``True``, it is assumed that ``bits`` are from a block
      0 packet.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * String of hex if the error correction was successful, else ``None``.
    * Number of errors found. Will be 10 or less for successfull
      FIS-B error correction (6 for ADS-B short, 7 for ADS-B long)
      or 98 for a failed correction.
    * Shift value that was successful, or -1 if not successful. Used as
      ``tryFirst`` on the next run.
  """
  # If a previous packet was decoded with a shift, use that shift as the
  # first attempt. It will almost always be successful.
  if tryFirst != -1:
    if tryFirst == 0:
      shiftedBits = bits
    elif tryFirst > 0:
      shiftedBits = shiftBits(bits, bitsBefore, tryFirst)
    else:
      shiftedBits = shiftBits(bits, bitsAfter, -tryFirst)

    errCorrectedHex, errs = packAndTest(rs, shiftedBits, f6b)
    if errs >= 0:
      return True, errCorrectedHex, errs, tryFirst

  # Try all shifts in ``SHIFT_BY_PROBABILITY`` list.
  for shift in SHIFT_BY_PROBABILITY:
    # Don't try a shift we already tried
    if shift == tryFirst:
      continue

    if shift == 0:
      shiftedBits = bits
    elif shift > 0:
      shiftedBits = shiftBits(bits, bitsBefore, shift)
    else:
      shiftedBits = shiftBits(bits, bitsAfter, -shift)
  
    if block0FixedBits:
      # For block zero fixed bits, force any fixed bits to 
      # the appropriate value. This will only be called for
      # block zero.
      # Note: This does not include 'position valid' in byte 6
      # (bit 47). This is always zero in reality, but standard states it
      # should be one.
      shiftedBits[48] = +10000   # UTC coupled
      shiftedBits[49] = -10000   # Reserved
      shiftedBits[50] = +10000   # App Data Valid
      shiftedBits[60] = -10000   # Reserved
      shiftedBits[61] = -10000   # Reserved
      shiftedBits[62] = -10000   # Reserved
      shiftedBits[63] = -10000   # Reserved
      shiftedBits[73] = -10000   # Reserved  (UAT Frame byte 2)
      shiftedBits[74] = -10000   # Reserved  (UAT Frame byte 2)
      shiftedBits[75] = -10000   # Reserved  (UAT Frame byte 2)
      
    errCorrectedHex, errs = packAndTest(rs, shiftedBits, f6b)
    if errs >= 0:
      return True, errCorrectedHex, errs, shift

  return False, None, 98, -1

def blockZeroTricks(bits, bitsBefore, bitsAfter, hexBlocks):
  """
  For a block zero packet, this will force the
  first 6 bytes to be a particular value, and/or set a number of bits
  to values that will always be true.

  The first 6 bytes of block zero contain the latitude and longitude of the
  ground station. With the receiver at a fixed location, only one value
  will usually be received. These can be forced into ``bits``. This is set with
  the ``--f6b`` flag. It is possible to provide more than one set of 6 bytes
  with the ``--f6b`` flag. If provided, all will be tried.

  Block 0 fixed bits are a set of bits always set (either 1 or 0) in block 0.
  This option is normally on, but can be turned off with the ``--nobzfb`` flag.

  Note that this routine is normally called after a normal decode of a packet is
  tried.

  Args:
    bits (nparray): Integers representing the current packet.
    bitsBefore (nparray): Integers representing the bits
      before the current packet.
    bitsAfter (nparray): Integers representing the bits
      after the current packet.
    hexBlocks (list): 6 item list containing 1 hex string for each decoded hex
      block, or ``None``.
    
  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * Array of ``hexblocks`` that was passed into the routine. It will be 
      unchanged if the decode was not a success, otherwise ``hexblocks[0]``
      will contain the hex characters for block 0.
    * Number of errors found. Will be 10 or less for successfull
      error correction or 98 for a failed correction.
  """
  # Using this in a file of 110951 all cause errors resulted in
  # this additional number of complete packet decodes:
  #   1st 6 byte fix only:  4908 (4.4%)
  #   block zero fix only:   756 (0.7%)
  #   combined:             5564 (5.0%)
  status, errCorrectedHex, errs, _ = tryShiftBits(rsFisb, bits, bitsBefore, \
        bitsAfter, -1, replace_f6b, block_zero_fixed_bits)
  if status:
    hexBlocks[0] = errCorrectedHex
    return True, hexBlocks, errs

  return False, hexBlocks, 98

def computeAverage1(bits):
  """
  Given a set of bits representing a single FIS-B block, compute the average of all
  1 values from the first 8 bytes and the last 20 bytes. Used as part of the
  process for finding runs of zero values.

  Args:
    bits (nparray): Numpy array of bits to check.

  Returns:
    float: Calculated average 1 value of the first 8 bytes and last 20 bytes.
  """
  # Isolate first 8 bytes and last 20 bytes.
  frontBits = bits[0:64]
  parityBits = bits[576:736]

  # Remove all values below zero
  frontOnes = np.delete(frontBits, np.where(frontBits < 0))
  parityOnes = np.delete(parityBits, np.where(parityBits < 0))
  
  # Calculate total number of samples.
  totalOnes = frontOnes.size + parityOnes.size
  
  # Sum the values of all the ones and divide by the number of samples to
  # get the average.
  ave = (np.sum(frontOnes) + np.sum(parityOnes)) / totalOnes
  return ave

def computeAverage0(bits):
  """
  Given a set of bits, compute the average of all the values below zero.

  Args:
    bits (nparray): Numpy array of bits to check.

  Returns:
    float: Calculated average zero value.
  """
  return (np.average(np.delete(bits, np.where(bits > 0))))

def computePercentAboveAveOne(bits, aveOne, adjFactor):
  """
  Given a set of ``bits``, compute the percentage of bits greater than
  ``aveOne`` times a percentage (``adjFactor``, usually 110%).

  Args:
    bits (nparray): Set of bits that the percentage will by computed from.
      This is subset of a FIS-B packet, usually 128 bits taken from the part
      of a complete FIS-B block that isn't the first 8 bytes, or last 20 parity 
      bytes.
    aveOne (float): Average 1 bit derived from the first 8 bytes and last 20 bytes
      of a FIS-B packet.
    adjFactor (float): ``aveOne`` is multiplied by this value to obtain a
      threshold value. Values above this threshold value are used to compute
      the percentage.

  Returns:
    str: Percentage of bits greater than ``aveOne`` * ``adjFactor``.
  """
  aveOneAdj = aveOne * adjFactor

  aboveAveBits = np.sum(np.where(bits >= aveOneAdj, 1, 0))
  ave = aboveAveBits / bits.size
  return ave

def fixZeros(block):
  """
  Given a FIS-B block, inspect the block and try to find a set of trailing zeros
  at the end of the block. If found, set the value of the zeros to the average
  zero for the entire block. This is called if a FIS-B block failed its first
  attempts at decoding.

  We first compute the average one value of the first 8 bytes of the block
  and the last 20 bytes of the block (parity bits). These bits will have a 
  number of one bits.

  Then we divide the rest of the block into 4 equal parts of 180 bits.
  Starting from the back of the block,
  we check the percentage of bits in the block that is greater than 110% of the
  average one value. If the percentage is > 2%, we assume the block is not a block
  of zeros.

  If it is a block of zeros, we continue backwards looking at each quarter section
  and computing if it is a block of zeros or not.

  As a last step, we take the last section that we think doesn't have zeros, and
  again starting at the end of the section, go back bit by bit until we find a
  bit that is greater than 87% of the average one value. When we find such a bit,
  we move at least 8 bits forward, and then potentially more bits forward, to align
  to a byte boundary.

  At this point, we will have a starting point where the block has zeros. We then set
  those bits to the average zero value for the entire packet.

  All of the somewhat arbitrary appearing numbers (110%, 87%, 2%) were obtained
  empirically using a large set of error packets.

  Args:
    block (nparray): Set of bits representing a single FIS-B block.

  Returns:
    tuple: Tuple containing:

    * ``True`` if we found a run of zeros and set them to a single value.
      Else ``False``.
    * Modified version of the ``block`` argument we were called with. Will have
      the zero run set to the average zero value of the block. If the first tuple
      item was ``False``, the contents of the ``block`` argument is returned
      unchanged.
  """
  # Out of 110951 errors, this repaired 15156 or 13.7%.
  # Ran at 228/sec.
  aveOne = computeAverage1(block)

  startValue = 576
  foundAnyZeros = False

  # Dived the block into 128 bit 'quarters', starting from the back.
  for i in range(576, 63, -128):
    sample = block[i-128:i]
    percentAboveAveOne = computePercentAboveAveOne(sample, aveOne, 128, 1.10)
    if percentAboveAveOne > 0.02:
      break
    if i != 64:
      startValue = i - 128
    else:
      i = 64

    foundAnyZeros = True

  # Try to get finer grain by going backwards and looking
  # for 1st value above 60% of 'percentAboveAveOne'.
  newStartValue = startValue
  if startValue != 64:
    threshold = aveOne * 0.87 # empirically derived
    for i in range(startValue - 1, startValue - 128, -1):
      if block[i] > threshold:
        newStartValue = i + 8

        # align to nearest byte
        rem = newStartValue % 8
        if rem != 0:
          newStartValue += 8 - rem

        if newStartValue > startValue:
          newStartValue = startValue
        else:
          foundAnyZeros = True

        break

  # We have zeros in the block, so set them to the average zero in the block.
  if foundAnyZeros:
    block[newStartValue:576] = computeAverage0(block)

  return foundAnyZeros, block

def decodeFisb(samples, offset, hexBlocks, hexErrs):
  """
  Given a FIS-B raw message, attempt to error correct all blocks.

  We loop over each block and determine the bits, bits before, and
  bits after. We then attempt to error correct using 
  ``tryShiftBits()`` which will try different combinations.

  We also employ various procedures such as seeing if a packet
  ends early (like an empty packet) where we do not need to error correct
  further blocks. Further processing will also be attempted if the intital
  attempt does not decode.

  Args:
    samples (nparray): Int 32 array of samples containing the bit
      before the actual sample and two bits after the normal sample.
    offset (int): The normal value is 1, which uses the starting bit
      (``samples[1]``) of the normal sample. Can also be 2, which looks at the set
      of bits after the current sample (``samples[2]``). Using this can result in a
      small, but significant increase, in error corrections.
    hexBlocks (list): 6 item list containing a hex string for each
      block, or ``None``. When first called with an offset of 1, will set hexBlocks
      to a list of 6 ``None`` values.
    hexErrs (list): List of 6 items containing integers representing
      the error count for each FIS-B block. When first called with an offset of 
      1, will be set to a list of 6 ``99`` values. ``99`` indicates no
      attempt to decode a block has been made.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * Updated version of ``hexBlocks`` which will be a 6 item list
      containing ``None`` for blocks that did not error correct, or a
      hex string with the error corrected value.
    * Updated version of ``hexErrs`` which will be a 6 item list
      with each element being number of errors found in the block
      (0-10), or ``98`` for a block that failed to error correct, and ``99`` if
      the block was not checked for errors.
  """
  # Create hexBlocks if first time call.
  if hexBlocks == None:
    hexBlocks = [None, None, None, None, None, None]

  # Create hexErrs if first time call.
  if hexErrs == None:
    hexErrs = [99, 99, 99, 99, 99, 99]

  shiftThatWorked = -1

  # Loop and try to error correct each block
  for block in range(0, 6):

    if hexBlocks[block] != None:
      continue

    bits, bitsBefore, bitsAfter = extractBlockBitsFisb(samples, offset, \
      block)
    
    # Shift bits
    status, errCorrectedHex, hexErrs[block], shift = tryShiftBits(rsFisb, \
        bits, bitsBefore, bitsAfter, shiftThatWorked)

    if status:
      # Start next block with the shift that worked.
      shiftThatWorked = shift

      hexBlocks[block] = errCorrectedHex

      # For block0, see if empty frame. We put this check here since
      # 'tryShiftBits' is the most likely opportunity to error correct a 
      # block zero, and this prevents us from even trying to error correct
      # other frames. Empty frames are very common.
      if block == 0:
        foundEmptyFrame, hexBlocks = block0ThoroughCheck(hexBlocks)
        if foundEmptyFrame:
          return True, hexBlocks, hexErrs

      continue

    # Extra checks for block 0
    if block == 0:
      if replace_f6b or block_zero_fixed_bits:
        status, hexBlocks, hexErrs[block] = blockZeroTricks(bits, \
            bitsBefore, bitsAfter, hexBlocks)
        if status:
          foundEmptyFrame, hexBlocks = block0ThoroughCheck(hexBlocks)
          if foundEmptyFrame:
            return True, hexBlocks, hexErrs
          continue
    
    # Look for blocks with trailing zeros
    if fix_trailing_zeros:
      foundAnyZeros, bits = fixZeros(bits)
      if foundAnyZeros:
        status, hexBlocks[block], hexErrs[block], shift = tryShiftBits(rsFisb, \
          bits, bitsBefore, bitsAfter, shiftThatWorked)
        if status:
          foundEmptyFrame, hexBlocks = block0ThoroughCheck(hexBlocks)
          if foundEmptyFrame:
            return True, hexBlocks, hexErrs
          continue

    # There are still blocks with errors

    # Nothing worked, abandon and try block0ThoroughCheck
    # Don't process other blocks.
    if not status:
      break
    
  # If there are None values in hexBlocks, nothing worked.
  if None in hexBlocks:
    # Do final check for early ending packet.
    status, hexBlocks = block0ThoroughCheck(hexBlocks)
    if status:
      return True, hexBlocks, hexErrs
    
    return False, hexBlocks, hexErrs
  
  # Otherwise, all blocks were error corrected.
  return True, hexBlocks, hexErrs

def decodeAdsb(samples, offset, isShort):
  """
  Given a ADS-B raw message, attempt to error correct all blocks.

  Args:
    samples (nparray): int32 array of samples containing the bit
      before the normal sample and two bits after the normal sample.
    offset (int): The normal value is 1, which uses the starting bit
      (``samples[1]``) of the normal sample. Can also be 2, which looks at the set
      of bits after the current sample (``samples[2]``).
    isShort (bool): ``True`` if we are trying to match a short ADS-B
      packet, otherwise ``False``.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * Hex string representing the error corrected message.
    * Number of errors found in the message (0-6 for ADS-B short,
      0-7 for ADS-B long, or ``98`` for a message that failed to error correct).
  """
  # Short and long ADS-B use different Reed-Solomon instances.
  if isShort:
    rs = rsAdsbS
  else:
    rs = rsAdsbL

  # Create the bits to use.
  bits, bitsBefore, bitsAfter = extractBlockBitsAdsb(samples, \
    offset, isShort)
  
  # Shift bits
  status, hexBlock, errs, _ = tryShiftBits(rs, bits, bitsBefore, bitsAfter, -1)

  if status:
    # These don't always decode correctly. Make sure that short messages are 
    # always payload type 0, and long messages are always payload types 1 to 6.
    # Also make sure the length of the hex string matches the payload type.
    hexBlockLen = len(hexBlock)
    byte0 = bytes.fromhex(hexBlock[0:2])[0]
    payloadTypeCode = (byte0 & 0xF8) >> 3

    if (payloadTypeCode == 0) and (hexBlockLen == 36):
      return True, hexBlock, errs
    elif (payloadTypeCode > 0) and (payloadTypeCode < 7) \
        and (hexBlockLen == 68):
      return True, hexBlock, errs

  return False, None, 98

def fisbHexErrsToStr(hexErrs):
  """
  Given an list containing error entries for each FIS-B block, return a
  string representing the errors. This will appear as a comment in either
  the result string, or the failed error message.

  Args:
    hexErrs (list): List of 6 items, one for each FIS-B block. Each entry
      will be the number of errors in the message (0-10), or 98 for a packet
      that failed, and 99 for a packet that wasn't tried.

  Returns:
    str: String containing display string for error messages.
  """
  return f'{hexErrs[0]:02}:{hexErrs[1]:02}:{hexErrs[2]:02}:{hexErrs[3]:02}:' + \
    f'{hexErrs[4]:02}:{hexErrs[5]:02}'

def fisbHexBlocksToHexString(hexBlocks):
  """
  Given a list of 6 items, each containing a hex-string of
  an error corrected block, return a string with all 6 hex strings
  concatinated. Used only for FIS-B.

  This only returns the bare hex-string with '+'
  prepended and no time, signal strength, or error count attached.

  Args:
    hexBlocks (list):  List of 6 strings containing a hex string for each
        of the 6 FIS-B hexblocks. Only called if all blocks error corrected
        successfully (i.e. there should be no ``None`` items).

  Returns:
    str: String with all hex data and a '+' prepended at the front.
    This function is only for FIS-B packets. No comment data is appended to
    the end.
  """
  return '+' + hexBlocks[0] + hexBlocks[1] + hexBlocks[2] + \
    hexBlocks[3] + hexBlocks[4] + hexBlocks[5]

def fisbHexBlocksFormatted(hexBlocks, signalStrengthStr, timeStr, hexErrs, \
   syncErrors):
  """
  Takes error corrected FIS-B message and produces final output string.

  Will concatenate all the hex blocks and add trailing error, signal strength
  and time information. Should be called only for a successfully error corrected
  FIS-B packet.

  Args:
    hexBlocks (list): 6 item list consisting of error corrected hex string for
      each block.
    signalStrengthStr (str): Already created signal strength string.
    timeStr (str): Already created time string.
    hexErrs (list): 6 item list with each element being the number
      of Reed-Solomon errors in the block.
    syncErrors (int): Number of errors found in the sync word.

  Returns:
    str: Final error corrected FIS-B string ready for display.
  """
  rsErrStr = fisbHexErrsToStr(hexErrs)

  return fisbHexBlocksToHexString(hexBlocks) + ';rs=' + str(syncErrors) + '/' + \
    rsErrStr + ';ss=' + signalStrengthStr + \
    ';t=' + timeStr

def decodeBase40(val):
  """
  Given a value representing a base-40 number, return a list of three items
  containing the decoded base-40 number. Each ``val`` holds 3 base-40
  characters.

  Used for decoding call signs.

  Args:
    val (int): Integer holding 3 base-40 characters.

  Returns:
    list: List of 3 items, each containing the numeric value of a base-40
    number.
  """
  if val == 0:
    return [0,0,0]

  ret = []
  while val:
    ret.append(int(val % 40))
    val //= 40

  while len(ret) < 3:
    ret.append(0)  

  return ret[::-1]

def decodeCallSign(byts):
  """
  Given a list of 6 bytes, each pair of bytes containing a base-40 number with 3
  characters, return a string representing a call sign. Result will have any
  trailing spaces stripped.

  Args:
    byts (list): List of 6 bytes. Each pair represents a base-40 number with 3
      characters.

  Returns:
    str: String with up to 9 characters, but any trailing whitespace is
    removed.
  """
  str = ''

  for i in range(0,6,2):
    val = (byts[i] << 8) | byts[i+1]
    valDigits = decodeBase40(val)

    for j in valDigits:
      str += BASE40_TABLE[j]
  
  return str.rstrip()

def miniAdsbDecode(hexBlock, timeStr):
  """
  Creates a tiny decode of a small portion of the ADS-B message which can 
  be printed with the hex decode of the packet in the comment section.
  It is enabled by specifying the ``--apd`` option.

  Example comments are: ::

    1.2.A79B5F/1.N59DF/8500/G
    0.0.A38101//2275/A
    1.0.A38101/1.1200/2325/A23:32:L7
    2.0.A38101//2275/A05:29-30:M11

  The first number is the payload type code. The second is the address
  qualifier (see DO-282B for what these values mean).
  Then the ICAO aircraft id (or something that stands in for it).
  The first item within the first pair of slashes is the emitter category
  (lots of options here, but most often: 0=unknown, 1=light acft,
  2=small acft, 3-6=heavy acft, 7=heli)
  and the callsign (usually the squawk, N-number, or flight callsign). This
  section is optional. The altitude is in the next set of slashes. The last
  portion with be either a ``G`` is this is a TIS-B/ADS-R message sent by
  a ground station, or ``A`` if a UAT message sent directly from an aircraft.

  If the message is a UAT message, and the aircraft has received any FIS-B messages
  from a ground station, the aircraft can show how many messages it has
  received from a particular ground station on a particular data channel.
  Each ground station has a 'TIS-B Site ID' in the range of 1 to 15. Each
  site id is allocated a particular set of channel numbers that it will
  transmit on. High power stations get 4 channels, medium stations 3, low
  power stations 2, and surface stations 1. Each second, there is (for lack
  of a better term) the 'data channel of the second'. This is determined by
  the number of seconds after UTC midnight. All ADS-B messages sent by aircraft
  will send the number of FIS-B packets they received from the 'data channel
  of the second' in the last 32 seconds. In the comment, this can look like:
  ``A05:29-30:M11``. ``A`` means sent by aircraft, ``05`` is the 'data channel
  of the second', ``29-30`` is the range of FIS-B packets received by the
  aircraft on that data channel in the last 32 seconds, and ``M11`` maps the
  data channel back to the TIS-B site id and the power of the station
  (``H``, ``M``, ``L``, ``S``).

  The full data channel FIS-B packets received section is not sent if the
  number of packets received is zero. Some planes never report any packets.
  
  Args:
    hexBlock (str): String of hex characters representing the ADS-B message.
    timeStr (str): String of a floating number representing the number of
      seconds and fractions of a seconds in UTC at which the packet was received.

  Returns:
    str: String containing a partial decode of the ADS-B message. See above 
    for formats.
  """
  adsbBytes = bytes.fromhex(hexBlock)
  payloadTypeCode = (adsbBytes[0] & 0xF8) >> 3
  addrQualifier = (adsbBytes[0] & 0x07)
  addr = hexBlock[2:8].upper()

  decodeStr = f'/{payloadTypeCode}.{addrQualifier}.{addr}'
  callSign = ''

  if payloadTypeCode in [1, 3]:
    # Decode emitter category and call sign
    callSign = decodeCallSign(adsbBytes[17:23])
    decodeStr += f'/{callSign[0]}.{callSign[1:]}/'
  else:
    decodeStr += '//'

  # Calculate altitude
  codedAltitude = (adsbBytes[10] << 4) | (adsbBytes[11] >> 4)
  if codedAltitude in [0, 4095]:
    if codedAltitude == 0:
      altitude = '?'
    else:
      altitude = '>101337.5'
  else:
    altitude = str((codedAltitude - 41) * 25)

  decodeStr += f'{altitude}/'

  if addrQualifier in [0, 1, 4, 5]:
    # Get the uplink feedback. If other than zero, display data channel
    uplinkFeedback = (adsbBytes[16] & 0x07)

    if uplinkFeedback != 0:
      # Figure out the data channel for this UTC second
      packetTime = datetime.fromtimestamp(float(timeStr))
      secsPastMidnightMod32 = int((packetTime - packetTime.replace(hour=0, \
          minute=0, second=0)).total_seconds()) % 32
      dataChan = 32 - secsPastMidnightMod32 + 1
      if dataChan == 33:
        dataChan = 1
      
      decodeStr += f'A{dataChan:02d}:{UPLINK_FEEDBACK_TABLE[uplinkFeedback]}'
    else:
      decodeStr += 'A'

    # If the uplinkFeedback is > 0, also add TIS-B site id information.
    if uplinkFeedback > 0:
      decodeStr += f':{FISB_DATA_CHANNEL[dataChan - 1]}'

  elif (addrQualifier in [2, 3]) and (payloadTypeCode >= 0) \
      and (payloadTypeCode <= 10):
    decodeStr += 'G'

  return decodeStr

def adsbHexBlockFormatted(hexBlock, signalStrengthStr, timeStr, errs, \
    syncErrors):
  """
  Takes error corrected ADS-B message and produces final output string.

  Will concatenate the error corrected hex block with the trailing error,
  signal strength, and time information. Should be called only for a successfully
  error corrected ADS-B packet.

  Args:
    hexBlock (str): Error corrected ADS-B message as a hex string.
    signalStrengthStr (str): Already created signal strength string.
    timeStr (str): Already created time string.
    errs (int): Number of Reed-Solomon errors detected.
    syncErrors (int): Number of errors found in the sync word.

  Returns:
    str: Final error corrected ADS-B string ready for display.
  """
  # If the --apd (ADS-B partial decode) flag is set, do a partial
  # decode
  decodeStr = ''
  hexBlockLen = len(hexBlock)
  
  if adsb_partial_decode:
    decodeStr = miniAdsbDecode(hexBlock, timeStr)

  return '-' + hexBlock + ';rs=' + str(syncErrors) + '/' + str(errs) + \
       f'{decodeStr};ss=' + signalStrengthStr + ';t=' + timeStr

def processFisbPacket(samples, timeStr, signalStrengthStr, syncErrors, \
    attrStr):
  """
  Takes raw FIS-B packet samples and will error correct to final hex string
  to be sent to standard output.

  If we are showing errors (``-ff``), will return error string instead (error strings 
  start with ``#`` and are ignored by other FIS-B processing code).

  Args:
    samples (nparray): Demodulated packet samples consisting of FIS-B data
      as well as 1 sample before and 2 samples after.
    timeStr (str): Already created time string.
    signalStrengthStr (str): Already created signal strength string.
    syncErrors (int): Number of errors found in the sync word.
    attrStr (str): String with attributes. Used primarily for matching
      an error file to a specific packet.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * Hex string representing the error corrected message. If the message
      was not error corrected, this will be the string of hex errors for
      each block in the form: 'n:n:n:n:n:n' where n is 0-10, 98, or 99.
      If we are displaying errors, this will be the error string ready
      for display.
  """
  # Starting offset is 1. This is where tha actual sample data begins.
  offset = 1

  # Start with simple sample error correction. This works most of the time.
  didErrCorrect, hexBlocks, hexErrs = decodeFisb(samples, offset, None, \
      None)

  if didErrCorrect:
    return didErrCorrect, fisbHexBlocksFormatted(hexBlocks, \
      signalStrengthStr, timeStr, hexErrs, syncErrors)

  # Try with added offset
  didErrCorrect, hexBlocks, hexErrs = decodeFisb(samples, offset + 1, \
      hexBlocks, hexErrs)

  if didErrCorrect:
    # Return formatted string (add 500 to num tries if we needed an offset)
    return didErrCorrect, fisbHexBlocksFormatted(hexBlocks, signalStrengthStr, \
      timeStr, hexErrs, syncErrors)
  
  # Did not error correct.
  hexErrsStr = fisbHexErrsToStr(hexErrs)

  if show_failed_fisb:

    failStr = f'#FAILED-FIS-B {syncErrors}/{hexErrsStr} ss={signalStrengthStr}' + \
      f' t={timeStr} {attrStr}'

    # Write to standard output.
    print(failStr, flush=True)

  return False, hexErrsStr

def processAdsbPacket(samples, timeStr, signalStrengthStr, syncErrors, \
    attrStr):
  """
  Takes raw ADS-B packet samples and will error correct to final hex string
  to be sent to standard output. This hex string will start with '``-``' and
  have a comment section at the end containing the time string, signal
  strength string, etc.

  If we are showing errors (``-fa``), will return error string instead (error strings 
  start with ``#`` and are ignored by other FIS-B processing code).

  Args:
    samples (nparray): Demodulated packet samples consisting of FIS-B data
      as well as 1 sample before and 2 samples after.
    timeStr (str): Already created time string.
    signalStrengthStr (str): Already created signal strength string.
    syncErrors (int): Number of errors found in the sync word.
    attrStr (str): String with attributes. Used primarily for matching
      an error file to a specific packet.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * Hex string representing the error corrected message. If the message
      was not error corrected, this will be ``None``.
      If we are displaying errors, this will be the error string.
    * ``True`` is this is a short ADS-B message (payload type code 0), or
      ``False`` if a long ADS-B message.
  """
  # Starting offset is 1. This is where tha actual sample data begins.
  offset = 1

  # Try to guess if this is a short message (1st 5 bits 0). We will try both
  # ways, but try to start with the most likely candidate.
  isShort = True #assume short
  for x in range(1, 10, 2):
    if samples[x] <= 0:
      isShort = False
      break
  
  # Error corrections are sorted by the % of the time they match. I.e. we try the
  # things most likely to work most of the time first.

  # normal 94.2%
  didErrCorrect, hexBlock, errs  = decodeAdsb(samples, offset, isShort)
  if didErrCorrect:
    return didErrCorrect, adsbHexBlockFormatted(hexBlock, signalStrengthStr, \
        timeStr, errs, syncErrors), isShort
  
  # opposite (we thought is was a long, but it was a short, or visa versa)
  # 2.9%
  didErrCorrect, hexBlock, errs = decodeAdsb(samples, offset, not isShort)
  if didErrCorrect:
    return didErrCorrect, adsbHexBlockFormatted(hexBlock, signalStrengthStr, \
        timeStr, errs, syncErrors), isShort
  
  # opposite offset (switch long for short (or visa versa) and add offset) 2.3%
  didErrCorrect, hexBlock, errs = decodeAdsb(samples, offset + 1, \
      not isShort)
  if didErrCorrect:
    return didErrCorrect, adsbHexBlockFormatted(hexBlock, signalStrengthStr, \
        timeStr, errs, syncErrors), isShort

  # offset (take our original data and increase the offset) 0.4%
  didErrCorrect, hexBlock, errs = decodeAdsb(samples, offset + 1, isShort)
  if didErrCorrect:
    return didErrCorrect, adsbHexBlockFormatted(hexBlock, signalStrengthStr, \
        timeStr, errs, syncErrors), isShort

  # Did not error correct.
  if show_failed_adsb:

    failStr = f'#FAILED-ADS-B {syncErrors}/{errs} ss={signalStrengthStr}' + \
      f' t={timeStr} {attrStr}'

    # Write to standard output.
    print(failStr, flush=True)

  return False, None, isShort

def main():
  """
  Process raw FIS-B and ADS-B (short and long) demodulated samples from
  standard input (usually from ``demod_978``).

  We read a fixed length attribute string containing information
  about the packet to follow. Then we read the packet and process it.

  Rinse and repeat.

  Output is sent to standard output. If indicated (``-se``), errors 
  can be saved seperately in files.
  """
  # Lowest signal level found for FIS-B and ADS-B.
  # These start out as higher than we will ever see.
  lowest_adsbs_level = 1000000000
  lowest_adsbl_level = 1000000000
  lowest_fisb_level = 1000000000
  
  try:
    while True:
      # We alternate reading attributes and packets

      # Read attributes
      attributes = sys.stdin.buffer.read(ATTRIBUTE_LEN)

      # Convert from bytes to string for parsing.
      attrStr = "".join(chr(x) for x in attributes)

      # Exit if nothing more to read (should only happen when
      # reading from file).
      if attrStr == '':
        break

      splitName = attrStr.split(".")

      # Create time string and signal strength string from the
      # file components.
      timeStr = splitName[0] + '.' + splitName[1][0:3]
      rawSignalStrength = round(int(splitName[3]) / 1000000.0, 2)
      signalStrengthString = str(rawSignalStrength)
            
      # Extract sync errors
      syncErrors = splitName[4]

      # Detect if this an a FIS-B packet ('F') or ADS-B packet ('A').
      if (splitName[2] == 'F'):
        isFisbPacket = True
        packetLength = PACKET_LENGTH_FISB
      else:
        isFisbPacket = False
        packetLength = PACKET_LENGTH_ADSB

      # Read packet as a set of bytes
      packetBuf = sys.stdin.buffer.read(packetLength)

      # Numpy will convert the bytes to int32's
      packet = np.frombuffer(packetBuf, np.int32)

      if isFisbPacket:
        didErrCorrect, resultStr = processFisbPacket(packet, timeStr, \
          signalStrengthString, syncErrors, attrStr)
      else:
        didErrCorrect, resultStr, isShort = processAdsbPacket(packet, timeStr, \
          signalStrengthString, syncErrors, attrStr)

      if didErrCorrect:
        # If printing lowest levels, print to stderr if this is the
        # lowest so far (for ADS-B and FIS-B independently).
        if show_lowest_levels:
          if isFisbPacket:
            if rawSignalStrength < lowest_fisb_level:
              lowest_fisb_level = rawSignalStrength
              print(f'lowest FIS-B     signal: {lowest_fisb_level}', \
                  flush=True, file=sys.stderr)
          else:
            if isShort:
              if rawSignalStrength < lowest_adsbs_level:
                lowest_adsbs_level = rawSignalStrength
                print(f'lowest ADS-B (S) signal: {lowest_adsbs_level}', \
                    flush=True, file=sys.stderr)
            else:
              if rawSignalStrength < lowest_adsbl_level:
                lowest_adsbl_level = rawSignalStrength
                print(f'lowest ADS-B (L) signal: {lowest_adsbl_level}', \
                    flush=True, file=sys.stderr)

        # Write to standard output.
        print(resultStr, flush=True)

      # Write error file only if we are printing an error string.
      # This lets us specify which error files to save.
      elif writingErrorFiles and \
          ((isFisbPacket and show_failed_fisb) or \
           ((not isFisbPacket) and show_failed_adsb)):

        # For a failed FIS-B packet, resultStr is a string of 
        # Reed-Solomon errors for each block. Add this to the
        # filename. This doesn't make sense for ADB-B packets
        # because the error count is always '99'.
        hexErrStr = ''
        if resultStr is not None:
          hexErrStr = '.' + resultStr

        # Write file to error directory.
        errPath = os.path.join(dir_out_errors, attrStr + hexErrStr + '.i32')
        with open(errPath, 'wb') as errFile:
          errFile.write(packetBuf)

  except KeyboardInterrupt:
    sys.exit(0)

def mainReprocessErrors(errorDir):
  """
  Reprocess any errors from the specified error directory
  (called when ``-re`` argument is given).

  This option will reprocess any errors that are in the
  error directory. This is used to study particular messages
  and create improved techniques for decoding.

  It does not delete any files in the directory and only
  one pass is made through the directory.

  If any message cannot be error corrected, it is not written to
  any error directory (even if specified).

  It will always print FIS-B and ADS-B errors.

  Args:
    errorDir (str): Directory where the error files are.
  """
  errFiles = glob.glob(os.path.join(errorDir, '*.i32'))

  for filen in errFiles:
    # find last index of '/'
    lastSlash = filen.rfind('/')

    attrStr = filen[lastSlash + 1:]

    splitName = attrStr.split(".")

    # Create time string and signal strength string from the
    # file components.
    timeStr = splitName[0] + '.' + splitName[1][0:3]
    signalStrengthString = str(round(int(splitName[3]) \
        / 1000000.0, 2))
      
    # Extract sync errors
    syncErrors = splitName[4]

    # Detect if this an a FIS-B packet ('F') or ADS-B packet ('A').
    if (splitName[2] == 'F'):
      isFisbPacket = True
      packetLength = PACKET_LENGTH_FISB
    else:
      isFisbPacket = False
      packetLength = PACKET_LENGTH_ADSB

    with open(filen, 'rb') as filePtr:
       # Read packet as a set of bytes
      packetBuf = filePtr.read(packetLength)

    # Numpy will convert the bytes to int32's
    packet = np.frombuffer(packetBuf, np.int32)

    if isFisbPacket:
      didErrCorrect, resultStr = processFisbPacket(packet, timeStr, \
        signalStrengthString, syncErrors, attrStr)
    else:
      didErrCorrect, resultStr, _ = processAdsbPacket(packet, timeStr, \
        signalStrengthString, syncErrors, attrStr)

    if didErrCorrect:
      # Write to standard output.
      print(resultStr, flush=True)

# Call main function
if __name__ == "__main__":

  hlpText = \
    """ec_978.py: Error correct FIS-B and ADS-B demodulated data from 'demod_978'.

Accepts FIS-B and ADS-B data supplied by 'demod_978' and send any output to
standard output.

ff, fa
======
By default will not produce any error messages for
bad packets. To show errored packets use '--ff' for FIS-B and '--fa' for
ADS-B.

se, re
======
The '--se' argument requires a directory where errors will be stored.
This directory should be used by the '--re' argument in a future run to reprocess
the errors. When the '--se' argument is given, you need to supply either '--fa', 
'--ff' or both to indicate the type of error(s) you wish to save.

When errors are reprocessed with '--re', the '--ff' and '--fa' arguments
are automatically set, and any '--se' argument is ignored.

nobzfb, noftz
=============
Normal operation is to attempt to correct known bad FIS-B packets. There
are two processes that are used if the packet is not decoded correctly. 
The first is to apply fixed bits (bits that are always 1 or 0) to the first
portion of a message. The other is to attempt to find runs of trailing zeros.
You can turn these behaviors off.

'--nobzfb' will turn off 'block zero fixed bits'. These are bits in FIS-B
block zero that have known fixed values (always 1 or always 0).

'--noftz' will prevent the recognition of a block with trailing zeros
(a string of zeros at the end).

f6b
===
If you have a fixed station and only receive one, or a few ground stations, you
can use the '--f6b' (first six bytes) flag to force those values in packets
that initially fail decoding. More than one value can be listed if enclosed in
quotes and separated by spaces. Examples of use would be:

    --f6b 3514c952d65c
    --f6b '3514c952d65c 38f18185534c'

ll
==
demod_978 uses a cutoff signal level to avoid trying to decode noise packets
that have the correct sync bits. '--ll' (lowest level) will display on
standard error the lowest level that either a FIS-B packet or ADS-B packet
(each have their own lowest level) decoded at. If a lower level is found, it
is display. This is a good way to find an optimal setting for demod_978's
'-l' switch.

apd
===
This stands for ADS-B partial decode and will add an additional comment to
the decode of ADS-B packets.

Example comments are: ::

  1.2.A79B5F/1.N59DF/8500/G
  0.0.A38101//2275/A
  1.0.A38101/1.1200/2325/A23:32:L7
  2.0.A38101//2275/A05:29-30:M11

The first number is the payload type code. The second is the address
qualifier (see DO-282B for what these values mean).
Then the ICAO aircraft id (or something that stands in for it).
The first item within the first pair of slashes is the emitter category
(lots of options here, but most often: 0=unknown, 1=light acft,
2=small acft, 3-6=heavy acft, 7=heli) and the callsign (usually the squawk,
N-number, or flight callsign). This section is optional. The altitude is in
the next set of slashes. The last portion with be either a 'G' is this is
a TIS-B/ADS-R message sent by a ground station, or 'A' if a UAT message
sent directly from an aircraft.

If the message is a UAT message, and the aircraft has received any FIS-B messages
from a ground station, the aircraft can show how many messages it has
received from a particular ground station on a particular data channel.
Each ground station has a 'TIS-B Site ID' in the range of 1 to 15. Each
site id is allocated a particular set of channel numbers that it will
transmit on. High power stations get 4 channels, medium stations 3, low
power stations 2, and surface stations 1. Each second, there is (for lack
of a better term) the 'data channel of the second'. This is determined by
the number of seconds after UTC midnight. All ADS-B messages sent by aircraft
will send the number of FIS-B packets they received from the 'data channel
of the second' in the last 32 seconds. In the comment, this can look like:
'A05:29-30:M11'. 'A' means sent by aircraft, '05' is the 'data channel
of the second', '29-30' is the range of FIS-B packets received by the
aircraft on that data channel in the last 32 seconds, and 'M11' maps the
data channel back to the TIS-B site id and the power of the station
('H', 'M', 'L', 'S').

The full data channel FIS-B packets received section is not sent if the
number of packets received is zero. Some planes never report any packets.
"""
  parser = argparse.ArgumentParser(description= hlpText, \
          formatter_class=RawTextHelpFormatter)
          
  parser.add_argument("--ff", \
    help='Print failed FIS-B packet information as a comment.', action='store_true')
  parser.add_argument("--fa", \
    help='Print failed ADS-B packet information as a comment.', action='store_true')
  parser.add_argument("--ll", \
    help='Print lowest levels of FIS-B and ADS-B signal levels.', \
    action='store_true')
  parser.add_argument("--nobzfb", \
    help="Don't repair block zero fixed bits.", action='store_true')
  parser.add_argument("--noftz", \
    help="Don't fix trailing zeros.", action='store_true')
  parser.add_argument("--apd", \
    help='Do a partial decode of ADS-B messages.', action='store_true')
  parser.add_argument("--f6b", required=False, \
    help='Hex strings of first 6 bytes of block zero.')
  parser.add_argument("--se", required=False, \
    help='Directory to save failed error corrections.')
  parser.add_argument("--re", required=False, \
    help='Directory to reprocess errors.')

  args = parser.parse_args()

  if args.ff:
    show_failed_fisb = True

  if args.fa:
    show_failed_adsb = True

  if args.ll:
    show_lowest_levels = True

  if args.nobzfb:
    block_zero_fixed_bits = False

  if args.noftz:
    fix_trailing_zeros = False

  if args.apd:
    adsb_partial_decode = True

  if args.se:
    dir_out_errors = args.se
    writingErrorFiles = True

  # If using 1st 6 bytes of block zero. May have more than one set of bytes
  # separated by whitspace.
  if args.f6b:
    replace_f6b = True

    hexStrList = args.f6b.split()
    
    f6bArrayLen = len(hexStrList)
    f6bArray = np.zeros((f6bArrayLen, 6), dtype=np.uint8)

    for i, hexstr in enumerate(hexStrList):
      # Catch non-hex values
      try:
        int(hexstr, 16)
      except ValueError:
        print('Illegal hex for --f6b', file=sys.stderr)
        sys.exit(1)

      # Length has to be 12
      if (len(hexstr) != 12):
        print('Hex string length must be 12', file=sys.stderr)
        sys.exit(1)

      # Convert hex to array.
      for j in range(0, 6):
        f6bArray[i][j] = int(hexstr[j*2:(j*2)+2], 16)

  # If reprocessing errors call mainReprocessErrors() else main()
  if args.re:
    # Writing error files doesn't work here. Unset if set
    writingErrorFiles = False

    # Since processing errors, assume -ff -fa
    show_failed_adsb = True
    show_failed_fisb = True
    
    mainReprocessErrors(args.re)
  else:
    main()
