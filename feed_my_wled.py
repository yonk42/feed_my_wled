#!/usr/bin/env python3

import sys
import argparse
import configparser
import socket
import struct
import numpy as np
from collections import deque

# command line: all optional, so plain `cat fifo | ./feed_my_wled.py` still works
parser = argparse.ArgumentParser(description="Feed raw s16le audio to WLED's audio-reactive UDP receiver.")
parser.add_argument("--config", default="feed_my_wled.conf",
                    help="preferences file (default: feed_my_wled.conf in the current directory)")
parser.add_argument("--input",
                    help="read audio from this FIFO/file instead of stdin; a FIFO is reopened whenever "
                         "a writer reconnects, so the script can run as a long-lived service")
args = parser.parse_args()

# load preferences file
config = configparser.ConfigParser()
if not config.read(args.config):
    sys.exit(f"Cannot read preferences file: {args.config}")

#load preferences
WLED_IP_ADDRESS = config.get("WLED", "WLED_IP_ADDRESS")
WLED_UDP_PORT = config.getint("WLED", "WLED_UDP_PORT")
sample_rate = config.getint("Audio", "sample_rate")
buffer_size = config.getint("Audio", "buffer_size")
chunk_size = config.getint("Audio", "chunk_size")

# def vars
previous_smoothed_level = 0.0
ring_buffer = deque(maxlen=buffer_size // chunk_size)  # Ringpuffer für Blöcke (standardmäßig leer)

# resolve the target once, not on every packet (matters for hostnames like wled.local)
WLED_ADDRESS = (socket.gethostbyname(WLED_IP_ADDRESS), WLED_UDP_PORT)

# create socket
udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

## functions
# calculating fft
def calculate_fft(audio_chunk, sample_rate):
    """
    Calc FFT for a Audioblock
    :param audio_chunk: Audiodata as Byte-Array.
    :param sample_rate: Samplerate of Audiodata.
    :return: Tuple (FFT-Ergebnisse for 16 Frequency bands, additional peaks).
    """
    try:
        # Convert to a numpy-Array
        audio_data = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.int32)

        # Calc Peak (Raw Level and Peak Level)
        raw_level = np.mean(np.abs(audio_data))
        peak_level = int((np.max(np.abs(audio_data)) / 32767) * 255)

        # Calc FFT and Amplitudes
        fft_result = np.abs(np.fft.rfft(audio_data))

        # Normalize from 0 to 255
        # (silence has a max of 0; np.interp would turn that into full scale)
        fft_max = np.max(fft_result)
        fft_normalized = fft_result * (255 / fft_max) if fft_max > 0 else np.zeros_like(fft_result)

        # Select first 16 frequency bands
        fft_values = fft_normalized[:16].astype(np.uint8)

        # Find dominating frequency
        freq_index = np.argmax(fft_result)
        fft_peak_frequency = freq_index * (sample_rate / len(audio_data))

        # sum of fft magnitudes
        fft_magnitude_sum = np.sum(fft_result)

        # return calced values
        return fft_values, raw_level, peak_level, fft_magnitude_sum, fft_peak_frequency

        #error handling
    except Exception as e:
        print(f"Error at calcing FFT: {e}")
        return None, 0, 0, 0, 0

# function for creating the udp package
def create_udp_packet(fft_values, raw_level, smoothed_level, peak_level, fft_magnitude_sum, fft_peak_frequency):
    """
    Creating udp-package in a wled compatible format
    :param fft_values: fft datas for 16 frequency bands
    :param raw_level: mean of audiosource
    :param smoothed_level: smoothed level of audiosource
    :param peak_level: mac peak of audiosource
    :param fft_magnitude_sum: sum of fft magnitudes
    :param fft_peak_frequency: dominant frequency of audiosignal
    :return: formated UDP-package
    """

    # Convert Values
    peak_level = int(peak_level)  # limit to uint8
    fft_values = [int(v) for v in fft_values]  # limit to uint8

    # Create package according to the WLED Protocol
    udp_packet = struct.pack('<6s2B2fBB16B2B2f',        # don't mess with that!
        b'00002',                   # Header (6 Bytes)
        0,0,                        # Gap (2 Bytes)
        float(raw_level),           # Raw Level (4 Bytes Float)
        float(smoothed_level),      # Smoothed Level (4 Bytes Float)
        peak_level,                 # Peak Level (1 Byte)
        0,                          # static 0 (1 Byte)
        *fft_values,                # FFT Result (16 Bytes)
        0,0,                        # Gap (2 Bytes)
        float(fft_magnitude_sum),   # FFT Magnitude (4 Bytes Float)
        float(fft_peak_frequency))  # FFT Major Peak (4 Bytes Float)
    return udp_packet

def send(packet):
    """Send a packet to WLED; an unreachable device must never stop the audio loop"""
    try:
        udp_socket.sendto(packet, WLED_ADDRESS)
    except OSError:
        pass

def reset_buffer():
    """Fill the delay buffer with silence"""
    ring_buffer.clear()
    for _ in range(ring_buffer.maxlen):
        ring_buffer.append(b"\x00" * chunk_size)

def read_streams():
    """Yield the binary stream(s) to read audio from"""
    if args.input is None:
        yield sys.stdin.buffer
        return
    while True:
        # opening a FIFO blocks until a writer (e.g. shairport-sync) connects
        with open(args.input, "rb") as stream:
            yield stream

# main function
def stream_audio_to_wled():
    """
    Reads audiodata from stream and send analyzed fft results to WLED
    """
    global previous_smoothed_level

    try:
        print(f"Start Pipe to WLED at ({WLED_ADDRESS[0]}:{WLED_ADDRESS[1]})...", flush=True)

        for stream in read_streams():
            reset_buffer()
            previous_smoothed_level = 0.0

            while True:
                # read datas from pipe
                audio_data = stream.read(chunk_size)
                if len(audio_data) < chunk_size:
                    break  # end of stream
                ring_buffer.append(audio_data)  # push data to buffer

                # Calc FFT and Peaks on the oldest block (that is the delay)
                fft_result = calculate_fft(ring_buffer[0], sample_rate)
                if fft_result[0] is None:
                    print("Unvalid FFT-Datas, skip actual block.")
                    continue

                # feed fft_result to its vars
                fft_data, raw_level, peak_level, fft_magnitude_sum, fft_peak_frequency = fft_result

                # Smooth the Peaks (Moving Average)
                smoothed_level = (0.8 * previous_smoothed_level) + (0.2 * raw_level)
                previous_smoothed_level = smoothed_level

                # Create UDP-Paket and send it to WLED
                udp_packet = create_udp_packet(fft_data, raw_level, smoothed_level, peak_level, fft_magnitude_sum, fft_peak_frequency)
                send(udp_packet)

            # stream ended (playback stopped): tell WLED it is quiet now
            send(create_udp_packet([0] * 16, 0, 0, 0, 0, 0))

    except KeyboardInterrupt:
        print("Audiostreaming closed.")
    finally:
        udp_socket.close()

# start
if __name__ == "__main__":
    stream_audio_to_wled()
