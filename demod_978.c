/** @file demod_978.c
 * @brief <b>Read samples from SDR and capture FIS-B and ADS-B packets.</b>

 * Reads raw SDR I/Q data from standard input at frequency of 978Mhz.
 * Assumes samples of 2 samples per data bit or 2,083,334 samples/sec. Samples
 * should be complex int 16 (CS16).
 * 
 * SDR samples are demodulated into packets of signed 32-bit integers. Attributes of each
 * packet (whether FIS-B or ADS-B, arrival time, signal strength) are
 * stored in a string and sent to standard output. This is followed up
 * with the actual packet data as signed 32-bit integers. These values are then
 * received and processed by the standard input of <b>ec_978.py</b>.
 * 
 * The decoding is divided between two programs since searching for sync
 * words in a large amount of data isn't what numpy is best at, but C
 * is amazingly fast at this. Likewise, python, using numpy, is super fast at
 * decoding data packets and trying various approaches to decode data that is
 * at the Nyquist limit.
 * 
 * <b>Usage:</b>
 * <p>
 *  <em>&lt;sdr-program 2083334 CS16&gt; | demod_978 &lt;arguments&gt;</em>
 * </p>
 *
 * <b>Arguments:</b>
 * <dl>
 *  <dt>-a</dt>
 *      <dd>Process ADS-B packets. One or both of -a and -f must be specified.
 *      Optional.</dd>
 * 
 *  <dt>-f</dt>
 *      <dd>Process FIS-B packets. One or both of -a and -f must be specified.
 *      Optional.</dd>
 * 
 *  <dt>-l &lt;float&gt;</dt>
 *      <dd>Set the noise cutoff level. Data samples are stronger than the 
 *      baseline noise level. This sets the minimum value required that
 *      demod will attempt to process a packet. The default is 0.9. The
 *      purpose of this is to decrease the number of false packets that
 *      are extracted from noise. If you are not sure if you are capturing
 *      all valid packets, set this to 0.0. The default value has no units,
 *      it was determined by evaluation of empirical data. It may vary
 *      based on SDR radio used, or SDR program used. Optional.</dd>
 * 
 *  <dt>-x</dt>
 *      <dd>If you are testing by feeding a file of already captured raw data
 *      in a file, set this argument. 'demod_978' attempts to get the correct
 *      timing when a packet arrived, so will figure out how many
 *      usecs past the time the sample was read to provide a correct
 *      value. This works fine for real-time data, but when dumping a file,
 *      it won't work. The -x argument will make sure the times on the packet
 *      filename will sort correctly and make sense. Optional.</dd>
 * </dl>
 * Data packets are sent to standard output preceeded with a 30 character
 * attribute string which has the following format:
 * <p>
 * <b>&lt;secs&gt;.&lt;usecs&gt;.&lt;t&gt;.&lt;level&gt;.&lt;syncerr&gt;</b>
 * 
 * <dl>
 * <dt>&lt;secs&gt;</dt>
 *  <dd>System time in seconds past epoch.</dd>
 * <dt>&lt;usecs&gt;</dt>
 *  <dd>Microseconds associated with &lt;secs&gt;.</dd>
 * <dt>&lt;t&gt;</dt>
 *  <dd>'F' for FIS-B packet. 'A' for ADS-B packet (either long or short).</dd>
 * <dt>&lt;level&gt;</dt>
 *  <dd>Absolute signal level. This is the raw signal level for the
 * sync code of the packet. In the -l argument, the level is
 * supplies in millionths. So the default 0.9 is really 900000.
 * If a signal of this level was received, it would
 * be shown as 00900000 in the attribute string.</dd>
 * <dt>&lt;syncerr&gt;</dt>
 *  <dd>Number of sync errors (will be 0 - 4).</dd>
 * </dl>
 * 
 * Example attribute string: <b>1638556942.209000.F.05182170.1 </b>
 * 
 * <em>CAUTION</em>: This program is designed for raw speed, so many items are put in
 * globals to avoid passing them around. 
 * 
 * <em>CAUTION</em>: The program makes unapologetic use of type-punning. As such, GCC
 * is required for compilation (or some other compiler that does the
 * correct thing for type-punning). Lots of normal coding
 * conventions are ignored in the interest of faster execution speeds.
 * 
 * <em>CAUTION</em>: <b>demod_978</b> assumes the data is reasonably filtered. If you are not
 * getting the performance you think you should be getting, capture some
 * data and view it with a tool like '<em>inspectrum</em>' to verify adequate
 * filtering. 
 * 
 * <em>CAUTION</em>: Assumes this is running on a little-endian architecture. It will
 * not run correctly on big-endian machines.
 * 
 */

#include <stdio.h>
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
#include <stdbool.h>
#include <sys/time.h>
#include <time.h>
#include <string.h>

/// Number of times a second we want to read data. This will affect 
/// the size of the raw sample read buffer. (<code>10</code>)
#define READS_PER_SECOND    10

/// Number of times we sample for each bit. This needs to be 2 for this
/// code. Do not change. (<code>2</code>)
#define SAMPLES_PER_BIT     2

/// Samples per second. Fixed at 2 * 1,041,667 = 2,083,334. (<code>2083334</code>)
#define SAMPLE_RATE         (SAMPLES_PER_BIT * 1041667)

/// Size of the buffer to store each raw read. Will be divisible by four.
/// Each sample consists of 2 16-bit words, or 4 bytes. (<code>833332</code>)
#define SAMPLE_BUFFER_BYTES  ((SAMPLE_RATE / READS_PER_SECOND) * 4)

/// Size of raw_buf as int16. Just SAMPLE_BUFFER_BYTES / 2. (<code>416666</code>)
#define SAMPLE_BUFFER_I16    (SAMPLE_BUFFER_BYTES / 2)

/// 36 bit valuefor syncing FIS-B. (<code>0x153225b1d</code>)
#define SYNC_FISB             0x153225b1d

/// 36 bit value for syncing ADS-B (bit inversion of FIS-B sync).
/// (<code>0xeacdda4e2</code>)
#define SYNC_ADSB             0xeacdda4e2

/// Number of int32 values to write with each fisb-packet. Includes
/// one sample before the actual packet and two samples after
/// the actual packet (so we can try the next sequence if needed).
/// (<code>8835</code>)
#define FISB_WRITE_INTS       ((4416 * 2) + 3)

/// Number of int32 values to write with each ADS-B packet.
/// We treat ADS-B short and ADS-B long the same. We try to detect the
/// difference during decoding. ADS-B short has 240 bits (144 + 96 parity)
/// (<code>771</code>)
#define ADSB_WRITE_INTS       ((384 * 2) + 3)

/// Each time sample represents 0.48 usecs. This is used to derive
/// the time of arrival of packets. The actual FIS-B bit rate is 0.96 usecs.
/// Since we sample at twice that, the sample rate is half of that, or
/// 0.48 usecs. (<code>0.48</code>)
#define SAMPLE_TIME_USECS     0.48

/// This is the length of the attribute string that we send before
/// every packet. (<code>30</code>)
#define ATTRIBUTE_LEN         30

/// running_total() keeps a record of the baseline signal level.
/// We only check sync when the signal is higher than this value.
/// This assumes lower values are basically random noise that passed the
/// sync test. Use the <code>-l</code> flag to change this at runtime
/// (the <code>-l</code> flag is
/// a float value that is 1/1000000 of <code>runningThreshold</code>).
int runningThreshold = 900000;

/// True is reading from file (<code>-x</code>).
bool readingFromFile = false;

/// Counter used if reading from file (<code>-x</code>). Used as ms time.
int readingFromFileCounter = 0;

/// Number of sync errors found in the last successful sync.
u_int8_t last_sync_errors;

/// True if producing fisb packets (<code>-f</code>)
bool doFisb = false;

/// True if producing adsb packets (<code>-a</code>)
bool doAdsb = false;

/// Holds read buffer for complex data from SDR.
/// We read the raw data as 4 bytes (two 16 bit ints representing a
/// complex number), then process it as two 16 bit integers via
/// type punning.
/// Assumes little endian and a compiler like GCC which allows
/// type-punning.
union {
  /// Holds raw input data as chars. Part of union.
  char raw_buf_bytes [SAMPLE_BUFFER_BYTES];

  /// Holds raw input data as int16_t values. Part of union.
  int16_t raw_buf_int [SAMPLE_BUFFER_I16];
} raw;

/// Holds write buffer for packets.
/// We process the data as int32s and write the data
/// as 4 bytes. The size of the buffer holds a 
/// FIS-B packet. ADS-B packets are smaller, thus fit inside too.
/// Assumes little endian and a compiler like GCC which allows
/// type-punning.
union {
  /// Holds output values as int32_t values. Part of union.
  int32_t fisb_buf_ints [FISB_WRITE_INTS];

  /// Holds output values as chars. Part of union.
  char fisb_buf_bytes [FISB_WRITE_INTS * 4];
} write_data;

/// Holds the number of characters from the last
/// read of 'raw'. Usually this is <code>SAMPLE_BUFFER_BYTES</code>,
/// but can be shorter if we are reading from a file and
/// this is the last read before EOF.
int raw_buf_int_size = 0;

/// Holds pointer to current index in <code>raw_buf_int[]</code>.
/// This is complex data, consisting of a real portion
/// followed by a complex portion.
int raw_buf_int_ptr = 0;

/* Variables related to determing packet arrival times */

/// This contains the number of 0.48 useconds intervals
/// from the time a raw block was read. Used to compute
/// time of packet arrival.
int time_sample_ptr = 0;

/// Set by read_block() at the time a new block is read.
/// Used to compute the actual time of packet arrival.
/// Contains the number of seconds since epoch.
int64_t time_secs;

/// Set by read_block() at the time a new block is read.
/// Used to compute the actual time of packet arrival.
/// Contains the number of useconds
/// for the current second in <code>time_secs</code>.
int64_t time_usecs;

/// System time is read into this structure just before a
/// raw block of data is read.
struct timeval time_of_read;

/* Variables related to the running total. */

/// Current running total. Proxy for signal strength.
/// See documentation for running_total() for details.
u_int32_t current_running_total = 0;

/// Array of last 72 samples for running total().
/// 72 denotes 72 samples which is the length of
/// 2 36-bit sync words.
/// Only used by running_total().
/// Could be static, but function is inline.
int32_t running_total_samples[72] = {0};

/// Index into <code>running_total_samples</code>.
/// Only used by running_total().
/// Could be static, but function is inline.
u_int8_t running_total_start = 0;

/// Total value of running_total not normalized.
/// <code>current_running_total</code> is this value / 72
/// (i.e. normalized).
/// Only used by running_total().
/// Could be static, but function is inline.
u_int32_t running_total_total = 0;

/* Variables related to detecting sync codes. */

/// Current 36-bit sync value for 'A' channel.
/// Manipulated by check_sync().
/// Global since function inline.
u_int64_t syncA = 0;

/// Current 36-bit sync value for 'B' channel.
/// Manipulated by check_sync()
/// Global since function inline.
u_int64_t syncB = 0;

/// Filehandle for buffered stdout.
FILE *stdoutbuf;

/**
 * @brief Update the current signal strength
 * 
 * running_total() keeps a running total of the absolute
 * value for the last 72 demodulated values. This acts as
 * a stand-in for signal strength.
 * 
 * For the last 72 values, we compute the average distance
 * from the zero level. We do this by adding the absolute value
 * of all samples then dividing by 72. Noise levels are usually
 * quite low and actual signal is considerably higher.
 * 
 * This value is used by process_sample() to see if we need to
 * check the sync.
 * 
 * @param new_sample Integer value of current sample.
 * 
 * Updates global: <code>running_total</code>.
 */
inline void running_total(int32_t new_sample) {
  // Use absolute value of samples.
  if (new_sample < 0)
    new_sample = -new_sample;
  
  // To make a fast running total, we keep all 72 values in
  // an array. For each new value we add it to the total and
  // the array and remove the earliest one.
  running_total_total = running_total_total - 
    running_total_samples[running_total_start] + new_sample;
  running_total_samples[running_total_start++] = new_sample;

  // Reset pointer to 0 if needed.
  if (running_total_start == 72)
    running_total_start = 0;

  // Normalize running total and update global.
  current_running_total = running_total_total / 72;
}


/**
 * @brief Check sync word for 4 or less errors.
 *
 * Uses Brian Kernighan's count 1-bits algorithm.
 *  
 * Note: One way to implement this and handle FIS-B
 * and ADS-B at the same time would be to count all the one
 * bits, and if the value is <= 4 you have a FIS-B value, and
 * if the value is >= 32 then you have a valid ADS-B value.
 * However, it is much faster to run each separately, since
 * they are guaranteed not to exceed more than 4 loops each
 * before exiting.
 * 
 * @param sync_val Current sync word.
 * @param is_fisb  True if FIS-B check, else ADS-B check.
 * @return True If this is a valid sync word, false otherwise.
 * 
 * Updates Globals: <code>last_sync_errors</code> (this is used only to put
 * the number of sync errors in the output filename).
 */
inline bool check_sync(u_int64_t sync_val, bool is_fisb) {
  u_int64_t xor_bits;
  
  // XOR to get a one bit for each bit that doesn't match
  // the sync word.
  if (is_fisb)
    xor_bits = (sync_val & 0xFFFFFFFFF) ^ SYNC_FISB;
  else
    xor_bits = (sync_val & 0xFFFFFFFFF) ^ SYNC_ADSB;

  int num_sync_errors = 0;

  // Brian Kernighan's count 1-bits algorithm.
  while (xor_bits != 0) {
    xor_bits &= xor_bits - 1;
    num_sync_errors++;

    // exit if more than 4 errors (1-bits)
    if (num_sync_errors > 4)
      return false;
  }

  last_sync_errors = num_sync_errors;
  return true;
}

/**
 * @brief Read a block of raw data from standard input.
 * 
 * Will exit for EOF or errors. Also, stores the time of read so
 * that it can be used to compute actual packet arrival time.
 * 
 * Update globals: <code>time_secs</code>, <code>time_usecs</code>,
 * <code>raw_buf_int_size</code>, <code>raw_buf_int_ptr</code>.
 */
void read_block() {
    // Get current time and store away. This is used later to compute
    // packet arrival times.
    gettimeofday(&time_of_read, NULL);
    time_secs = (int64_t) time_of_read.tv_sec;
    time_usecs = (int64_t) time_of_read.tv_usec;

    // Read block of data from standard input.
    raw_buf_int_size = read(STDIN_FILENO, raw.raw_buf_bytes,
          SAMPLE_BUFFER_BYTES);
    switch (raw_buf_int_size) {
      case 0:
        // EOF, just exit
        close(STDIN_FILENO);
        exit(EXIT_SUCCESS);
      case -1:
        // Error, print error message and exit
        fprintf(stdout, "Error occurred reading file\n");
        exit(EXIT_FAILURE);
    }

    // Size returned was number of bytes, make that the number of int16s.
    raw_buf_int_size /= 2;

    // reset pointer
    raw_buf_int_ptr = 0;
}

/**
 * @brief Demodulate one input sample.
 * 
 * We use the following equation to demodulate:
 * 
 * <code>sample = (I[n-2] * Q[n]) - (I[n] * Q[n-2])</code>
 * 
 * This is based on 2 samples per bit (Nyquist limit). For higher
 * sample rates you would want a higher n. Empirically, n = 2
 * is the optimal value for our constraints.
 * 
 * We don't normalize by dividing by <code>I[n]^2 + Q[n]^2</code> because
 * it does tend to slow things down. Empirically, if you do
 * normalize, you will get a small number of additional decodes
 * that don't require any correction, but all of these will
 * correct with manipulation.
 * 
 * Will read next block of raw data after processing 
 * current sample if we are at the end of the buffer.
 * 
 * @return int32_t Next demodulated sample.
 * 
 * Updates globals: <code>time_sample_ptr</code>, <code>time_secs</code>,
 * <code>time_usecs</code>, <code>running_total</code>.
 */
int32_t demod_one() {
  // Future demodulated sample.
  int32_t sample;
  
  // We need to know if this is the end of the buffer for two
  // reasons:
  //   1) Store current time of current raw sample since it will
  //      wiped out with next sample.
  //   2) Trigger reading the next sample from input.
  bool isEndOfBuffer = false;
  
  // Calculation requires knowing current sample (N0) and the sample
  // two samples ago (N2). N1 (previous sample) is just used as an
  // intermediary.
  static int32_t demodN0real = 0;
  static int32_t demodN0imag = 0;
  static int32_t demodN1real = 0;
  static int32_t demodN1imag = 0;
  static int32_t demodN2real = 0;
  static int32_t demodN2imag = 0;

  // 'time_sample_ptr' is the number of actual data bits (at .96 usecs
  // per bit) we are processing. This is used to produce the number
  // of usecs from the time the block was read.
  time_sample_ptr = raw_buf_int_ptr / 2;

  // check if end of buffer. Stored away since we use it again later.
  if (raw_buf_int_ptr + 2 == raw_buf_int_size) {
    isEndOfBuffer = true;

    // Case where we will read a new disk block before
    // we return the sample. This will change the times, so
    // we store the time.
    time_usecs = (int64_t) time_of_read.tv_usec;
    time_secs = (int64_t) time_of_read.tv_sec;
  }
  
  // Read values from buffer using type-punning.
  demodN0real = (int32_t) raw.raw_buf_int[raw_buf_int_ptr++];
  demodN0imag = (int32_t) raw.raw_buf_int[raw_buf_int_ptr++];

  // Demodulate and compute sample value.
  sample = (demodN2real * demodN0imag) - (demodN0real * demodN2imag);
    
  // Assign values for next time.
  demodN2real = demodN1real;
  demodN2imag = demodN1imag;
  demodN1real = demodN0real;
  demodN1imag = demodN0imag;

  // See if we are at end of buffer.
  if (isEndOfBuffer) {
    // Read next block of raw data.
    read_block();
  }

  // Update running total with new sample.
  running_total(sample);

  return sample;
}

/**
 * @brief Write demodulated packet (without sync) to standard output.
 * 
 * We send a string of attributes of fixed length (<code>ATTRIBUTE_LEN</code>)
 * followed by the packet sample.
 * 
 * Sample written consists of one sample before the actual data block
 * and two after. This lets the final decode program calculate
 * the next shifted sample, and determine optimum sampling points.
 * 
 * May terminate if errors detected during writing.
 * 
 * @param isFisb True if FIS-B packet, else ADS-B packet.
 */
void write_packet(bool isFisb) {
  char attributes[61];

  // Compute actual time. The time is recorded at each read. Each
  // sample is 0.48 usecs. It is stored for each sample as well as the
  // index pointer. At the end of the sync, this would be 72 samples 
  // we have to go backwards to start the timing at the beginning of the
  // sync.
  int64_t usecs_after_sample;
  int64_t actual_usecs;

  if (readingFromFile) {
    actual_usecs = readingFromFileCounter++ * 1000;
    if (readingFromFileCounter == 1000) {
      readingFromFileCounter = 0;
    }
  }
  else {
    usecs_after_sample = (int64_t) ((float) (time_sample_ptr * 
        SAMPLE_TIME_USECS) - (72.0 * SAMPLE_TIME_USECS));
    actual_usecs = time_usecs + usecs_after_sample;

    if (actual_usecs > 1000000) {
      time_secs++;
      actual_usecs = actual_usecs - 1000000;
    } else if (actual_usecs < 0) {
      time_secs--;
      actual_usecs = 1000000 + actual_usecs;
    }
  }

  // Determine type of packet.
  char typeChar = 'F';
  if (!isFisb) {
    typeChar = 'A';
  }
  
  // Write packet attributes to string. Double check current_running_total
  // is in bounds. If not, force it in bounds.
  if (current_running_total >= 1000000000) {
    current_running_total = 99999999;
  }
  sprintf(attributes,"%lu.%06ld.%c.%08d.%d", time_secs,
      actual_usecs, typeChar, current_running_total, last_sync_errors);

  // double to check to make sure this is ATTRIBUTE_LEN
  if (strlen(attributes) != ATTRIBUTE_LEN) {
    fprintf(stderr, "Got %ld for attribute length, not %d. Attributes: '%s'\n",
        strlen(attributes), ATTRIBUTE_LEN, attributes);
    exit(EXIT_FAILURE);
  }

  // Write ATTRIBUTE_LEN bytes of attribute information
  int attrBytesWritten = fwrite(attributes, 1, ATTRIBUTE_LEN, stdoutbuf);
  if (attrBytesWritten != ATTRIBUTE_LEN) {
    fprintf(stderr, "Writing attribute, got %ld for attribute length, not %d\n",
        strlen(attributes), ATTRIBUTE_LEN);
    exit(EXIT_FAILURE);
  }

  // Get ready to write packet.
  int bytesToWrite;
  
  // written this way for optimization. Much slower if variable used
  // vs constant.
  if (isFisb) {
    // write out packet data (FIS-B)
    for (int i = 0; i < FISB_WRITE_INTS; i++) {
      write_data.fisb_buf_ints[i] = demod_one();
    }

    bytesToWrite = FISB_WRITE_INTS * 4;
  }
  else {
    // write out packet data (ADS-B)
    for (int i = 0; i < ADSB_WRITE_INTS; i++) {
      write_data.fisb_buf_ints[i] = demod_one();
    }

    bytesToWrite = ADSB_WRITE_INTS * 4;
  }

  // Write packet and make sure we wrote the correct number of bytes.
  int bytes_written = fwrite(write_data.fisb_buf_bytes, 1, bytesToWrite, stdoutbuf);
  if (bytes_written != bytesToWrite) {
    fprintf(stderr, "Got %d writing file\n", bytes_written);
    exit(EXIT_FAILURE);
  }
}

/**
 * @brief Process one or two sample values.
 * 
 * Will read a sample and update channel 'A' sync.
 * If the signal strength is high enough, will check
 * the sync for validity. If valid (4 or less errors), 
 * will write the next samples to disk (either FIS-B or
 * ADS-B depending on which sync code matched).
 * 
 * If no sync match, will read another sample and do the
 * same for the 'B' channel.
 * 
 * Note: If we match a packet, the sync codes (both channels)
 * are zeroed out. Also, we will continue looking for sync
 * after the end of the packet. Not after the next sample.
 * This potentially misses some samples. This does not seem
 * to be an issue.
 * 
 * The common case where the next channel will be the one
 * with the better decode is handled by sending two additional
 * samples at the end of the normal block which allows for
 * next channel decoding.
 * 
 * Update globals: <code>syncA</code> and <code>sync</code>B.
 */
inline void process_sample() {
  // Get demodulated sample.
  int32_t sample = demod_one();
  
  // Update 'A' sync.
  if (sample > 0) {
    syncA = (syncA << 1) | 1;
  } else {
    syncA = (syncA << 1);
  }

  // If signal strength high enough, check the
  // sync and process a packet if indicated.
  if (current_running_total > runningThreshold) {
    if (doFisb && check_sync(syncA, true)) {
      write_packet(true);
      syncA = 0;
      syncB = 0;
      return;
    }
    else if (doAdsb && check_sync(syncA, false)) {
      write_packet(false);
      syncA = 0;
      syncB = 0;
      return;
    }
  }

  // Get another demodulated sample.
  sample = demod_one();

  // Update 'B' sync.
  if (sample > 0) {
    syncB = (syncB << 1) | 1;
  } else {
    syncB = (syncB << 1);
  }

  // If signal strength high enough, check the
  // sync and process a packet if indicated.
  if (current_running_total > runningThreshold) {
    if (doFisb && check_sync(syncB, true)) {
      write_packet(true);
      syncA = 0;
      syncB = 0;
      return;
    }
    else if (doAdsb && check_sync(syncB, false)) {
      write_packet(false);
      syncA = 0;
      syncB = 0;
      return;
    }
  }
}

/**
 * @brief Print usage information and exit.
 * 
 * @param progName Program name (i.e. argv[0])
 */
void printUsageThenExit(const char *progName) {
  fprintf(stderr, "Usage: %s [-f] [-a] [-x] [-l level] -d <directory>]\n", progName);
  fprintf(stderr, "-a          Capture ADS-B packets only.\n");
  fprintf(stderr, "-f          Capture FIS-B packets only.\n");
  fprintf(stderr, "             -if neither -a or -f, both FIS-B and ADS-B are processed.\n");
  fprintf(stderr, "-l <float>  Set noise cutoff to <float>.\n");
  fprintf(stderr, "             -default is 0.9.\n");
  fprintf(stderr, "-x          Set if reading from file and not real-time.\n");
  fprintf(stderr, "             -will make arrival times unique.\n");
  exit(EXIT_FAILURE);
}

/**
 * @brief Main program. Decode arguments and process samples.
 * 
 * Reads in initial block of raw data, then loops processing 
 * samples.
 * 
 * @param argc Number of arguments.
 * @param argv List of arguments.
 * @return int Error code.
 */
int main(int argc, char *argv[]) {
  int opt;

  // handle options
  while ((opt = getopt(argc, argv, "faxl:")) != -1) {
    switch (opt) {
      case 'f':
        doFisb = true;
        break;
      case 'a':
        doAdsb = true;
        break;
      case 'l':
        runningThreshold = (int)(atof(optarg) * 1000000.0);
        break;
      case 'x':
        readingFromFile = true;
        break;
      default:
        printUsageThenExit(argv[0]);
    }
  }

  // Must be processing one or both of FIS-B and ADS-B packets.
  if (doFisb && doAdsb) {
    fprintf(stderr, "Only one of -f and -a must be set. Use no flags for both ADS-B FIS-B to be processed.\n\n");
    printUsageThenExit(argv[0]);
  } else if (!doFisb && !doAdsb) {
    doFisb = true;
    doAdsb = true;
  }

  // Threshold must be positive.
  if (runningThreshold < 0) {
    fprintf(stderr, "Level (-l) argument must be positive.'/'\n\n");
    printUsageThenExit(argv[0]);
  }
 
  // Since writing to stdout, open buffered binary writer.
  stdoutbuf = fdopen(dup(STDOUT_FILENO), "wb");

  // Read initial block.
  read_block();

  // Loop forever reading samples.
  while (1) {
    process_sample();
  }  
}
