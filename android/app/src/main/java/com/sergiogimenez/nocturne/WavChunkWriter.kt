package com.sergiogimenez.nocturne

import java.io.File
import java.io.RandomAccessFile

class WavChunkWriter(
    directory: File,
    private val fileName: String,
    private val sampleRate: Int,
) {
    private val partFile = File(directory, "$fileName.part")
    private val output = RandomAccessFile(partFile, "rw")
    var sampleCount: Long = 0
        private set

    init {
        output.setLength(0)
        output.writeBytes("RIFF")
        SessionStore.writeLittleEndianInt(output, 36)
        output.writeBytes("WAVEfmt ")
        SessionStore.writeLittleEndianInt(output, 16)
        SessionStore.writeLittleEndianShort(output, 1)
        SessionStore.writeLittleEndianShort(output, 1)
        SessionStore.writeLittleEndianInt(output, sampleRate.toLong())
        SessionStore.writeLittleEndianInt(output, sampleRate * 2L)
        SessionStore.writeLittleEndianShort(output, 2)
        SessionStore.writeLittleEndianShort(output, 16)
        output.writeBytes("data")
        SessionStore.writeLittleEndianInt(output, 0)
    }

    fun write(samples: ShortArray, offset: Int, length: Int) {
        val bytes = ByteArray(length * 2)
        repeat(length) { index ->
            val value = samples[offset + index].toInt()
            bytes[index * 2] = (value and 0xff).toByte()
            bytes[index * 2 + 1] = (value shr 8 and 0xff).toByte()
        }
        output.write(bytes)
        sampleCount += length
    }

    fun closeAndCommit(): File {
        SessionStore.repairWavHeader(partFile, sampleCount, sampleRate)
        output.close()
        val finalFile = File(partFile.parentFile, fileName)
        check(partFile.renameTo(finalFile)) { "Could not finalize $fileName" }
        return finalFile
    }

    fun closeAfterFailure() {
        runCatching { output.fd.sync() }
        runCatching { output.close() }
    }
}
