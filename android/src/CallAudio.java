// CallAudio: runs on the phone as the adb shell user (via app_process) and uses Android's
// call-audio interception system API (CALL_AUDIO_INTERCEPTION, Android 13+) to:
//   - extract the remote party's voice from a live PSTN call (downlink, "Telephony Rx")
//   - inject audio into the call so the remote party hears it (uplink, "Telephony Tx")
//
// Usage (from the computer):
//   adb push callaudio.dex /data/local/tmp/
//   adb shell CLASSPATH=/data/local/tmp/callaudio.dex app_process / CallAudio probe
//   adb shell CLASSPATH=/data/local/tmp/callaudio.dex app_process / CallAudio test <seconds> [up.wav] [redirect]
//   adb shell CLASSPATH=/data/local/tmp/callaudio.dex app_process / CallAudio bridge [uplinkRate] [audio|events]
//
// Logs go to stderr/stdout; test mode writes the captured downlink to /data/local/tmp/down.wav.
//
// Bridge mode listens on the abstract local socket "callaudio" (reach it from the computer with
// `adb forward tcp:PORT localabstract:callaudio`). Frames in both directions are:
//   [type:1 byte][length:2 bytes big-endian][payload]
// phone -> computer: 1 = downlink PCM16 mono 16 kHz (remote party), 3 = event (UTF-8 text)
// computer -> phone: 1 = uplink PCM16 mono at uplinkRate (played into the call), 2 = flush queued uplink,
//                    4 = mic mute (payload 1 byte: 0/1), 0 = stop
// "events" mode skips call-audio interception and only reports call state (for phones whose call
// audio is carried over Bluetooth to the computer instead).

import android.app.Application;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Build;
import android.os.Looper;
import android.os.Process;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataOutputStream;
import java.io.DataInputStream;
import java.io.EOFException;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;

public final class CallAudio {
    static final int RATE = 16000;
    static final String PKG = "com.android.shell";

    // ---- Context workarounds (same approach scrcpy uses to act as the shell package) ----

    static Class<?> atClass;
    static Object activityThread;

    static final class FakeContext extends ContextWrapper {
        FakeContext(Context base) { super(base); }
        @Override public String getPackageName() { return PKG; }
        @Override public String getOpPackageName() { return PKG; }
        @Override public AttributionSource getAttributionSource() {
            return new AttributionSource.Builder(Process.SHELL_UID).setPackageName(PKG).build();
        }
        @Override public Context getApplicationContext() { return this; }
    }

    static FakeContext fakeContext;

    static void setupContext() throws Exception {
        atClass = Class.forName("android.app.ActivityThread");
        Constructor<?> c = atClass.getDeclaredConstructor();
        c.setAccessible(true);
        activityThread = c.newInstance();
        set(atClass, null, "sCurrentActivityThread", activityThread);
        Field sys = atClass.getDeclaredField("mSystemThread");
        sys.setAccessible(true);
        sys.setBoolean(activityThread, true);

        // mBoundApplication.appInfo.packageName = shell
        Class<?> abd = Class.forName("android.app.ActivityThread$AppBindData");
        Constructor<?> abdc = abd.getDeclaredConstructor();
        abdc.setAccessible(true);
        Object bindData = abdc.newInstance();
        ApplicationInfo ai = new ApplicationInfo();
        ai.packageName = PKG;
        set(abd, bindData, "appInfo", ai);
        set(atClass, activityThread, "mBoundApplication", bindData);

        // Samsung's CompatSandbox reads ActivityThread.getConfiguration() while creating the system
        // context, which NPEs unless mConfigurationController is set.
        try {
            Class<?> ccClass = Class.forName("android.app.ConfigurationController");
            Class<?> atiClass = Class.forName("android.app.ActivityThreadInternal");
            Constructor<?> ccc = ccClass.getDeclaredConstructor(atiClass);
            ccc.setAccessible(true);
            set(atClass, activityThread, "mConfigurationController", ccc.newInstance(activityThread));
        } catch (Throwable t) {
            log("ConfigurationController workaround skipped: " + t);
        }

        Method gsc = atClass.getDeclaredMethod("getSystemContext");
        gsc.setAccessible(true);
        fakeContext = new FakeContext((Context) gsc.invoke(activityThread));

        Application app = new Application();
        set(ContextWrapper.class, app, "mBase", fakeContext);
        set(atClass, activityThread, "mInitialApplication", app);

        // Normal app processes register the telephony/media service managers at startup; without
        // this, TelephonyManager NPEs in app_process.
        try {
            Method init = atClass.getDeclaredMethod("initializeMainlineModules");
            init.setAccessible(true);
            init.invoke(null);
        } catch (Throwable t) {
            log("initializeMainlineModules skipped: " + root(t));
        }
    }

    static void set(Class<?> cls, Object obj, String field, Object value) throws Exception {
        Field f = cls.getDeclaredField(field);
        f.setAccessible(true);
        f.set(obj, value);
    }

    static AudioManager audioManager() throws Exception {
        Constructor<AudioManager> c = AudioManager.class.getDeclaredConstructor(Context.class);
        c.setAccessible(true);
        return c.newInstance(fakeContext);
    }

    static Object call(Object target, String name, Class<?>[] types, Object... args) throws Exception {
        Method m = target.getClass().getMethod(name, types);
        return m.invoke(target, args);
    }

    static String modeName(int m) {
        switch (m) {
            case 0: return "NORMAL"; case 1: return "RINGTONE"; case 2: return "IN_CALL";
            case 3: return "IN_COMMUNICATION"; case 4: return "CALL_SCREENING";
            case 5: return "CALL_REDIRECT"; case 6: return "COMMUNICATION_REDIRECT";
            default: return String.valueOf(m);
        }
    }

    static void log(String s) { System.out.println("[callaudio] " + s); System.out.flush(); }

    static Throwable root(Throwable t) { while (t.getCause() != null) t = t.getCause(); return t; }

    // ---- Modes ----

    static void probe(AudioManager am) {
        log("SDK=" + Build.VERSION.SDK_INT + " model=" + Build.MODEL + " uid=" + Process.myUid());
        log("audio mode=" + modeName(am.getMode()));
        try {
            Object r = call(am, "isPstnCallAudioInterceptable", new Class<?>[0]);
            log("isPstnCallAudioInterceptable=" + r);
        } catch (Throwable t) {
            log("isPstnCallAudioInterceptable FAILED: " + root(t));
        }
        for (Method m : AudioManager.class.getDeclaredMethods()) {
            String n = m.getName();
            if (n.matches("(?i).*(pstn|injection|extraction|redirect|intercept).*")) log("  api: " + m);
        }
    }

    static void test(AudioManager am, int seconds, String upWav, boolean redirect) throws Exception {
        probe(am);
        int originalMode = am.getMode();
        if (redirect) {
            am.setMode(AudioManager.MODE_CALL_REDIRECT);
            Thread.sleep(300);
            log("setMode(CALL_REDIRECT) -> mode now " + modeName(am.getMode()));
        }

        AudioFormat inFmt = new AudioFormat.Builder().setSampleRate(RATE)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_IN_MONO).build();
        AudioFormat outFmt = new AudioFormat.Builder().setSampleRate(RATE)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build();

        AudioRecord rec = null;
        AudioTrack track = null;
        try {
            rec = (AudioRecord) call(am, "getCallDownlinkExtractionAudioRecord", new Class<?>[]{AudioFormat.class}, inFmt);
            log("downlink AudioRecord OK state=" + rec.getState());
        } catch (Throwable t) {
            log("downlink extraction FAILED: " + root(t));
        }
        try {
            track = (AudioTrack) call(am, "getCallUplinkInjectionAudioTrack", new Class<?>[]{AudioFormat.class}, outFmt);
            log("uplink AudioTrack OK state=" + track.getState());
        } catch (Throwable t) {
            log("uplink injection FAILED: " + root(t));
        }

        // Uplink: play the given 16 kHz mono PCM16 WAV into the call, in a background thread.
        final AudioTrack upTrack = track;
        Thread up = null;
        if (upTrack != null && upWav != null) {
            up = new Thread(() -> {
                try (DataInputStream in = new DataInputStream(new FileInputStream(upWav))) {
                    in.skipBytes(44);
                    upTrack.play();
                    byte[] buf = new byte[RATE / 50 * 2];  // 20 ms
                    int n, total = 0;
                    while ((n = in.read(buf)) > 0) total += upTrack.write(buf, 0, n);
                    log("uplink: wrote " + total + " bytes (" + (total / 2 / (double) RATE) + " s)");
                } catch (Throwable t) {
                    log("uplink thread FAILED: " + root(t));
                }
            });
            up.start();
        }

        // Monitor: record what the phone actually transmits (VOICE_UPLINK) to verify injection reaches the network.
        Thread mon = null;
        try {
            final AudioRecord monRec = new AudioRecord.Builder()
                    .setAudioSource(android.media.MediaRecorder.AudioSource.VOICE_UPLINK)
                    .setAudioFormat(inFmt).setContext(fakeContext).build();
            log("uplink monitor AudioRecord OK state=" + monRec.getState());
            mon = new Thread(() -> {
                try { capture(monRec, new File("/data/local/tmp/upmon.wav"), seconds, "upmon"); }
                catch (Throwable t) { log("upmon FAILED: " + root(t)); }
                finally { monRec.release(); }
            });
            mon.start();
        } catch (Throwable t) {
            log("uplink monitor unavailable: " + root(t));
        }

        // Downlink: capture `seconds` of the remote party's audio to a WAV, logging loudness per second.
        if (rec != null) {
            capture(rec, new File("/data/local/tmp/down.wav"), seconds, "downlink");
        } else {
            Thread.sleep(seconds * 1000L);
        }

        if (mon != null) mon.join(3000);
        if (up != null) up.join(2000);
        if (track != null) { try { track.stop(); } catch (Throwable ignored) {} track.release(); }
        if (rec != null) rec.release();
        if (redirect) {
            am.setMode(originalMode);
            log("restored mode -> " + modeName(am.getMode()));
        }
    }

    // ---- Bridge: stream call audio to/from the computer over a local socket ----

    static final int T_STOP = 0, T_AUDIO = 1, T_FLUSH = 2, T_EVENT = 3, T_MUTE = 4;

    static void sendFrame(DataOutputStream out, int type, byte[] buf, int len) throws IOException {
        synchronized (out) {
            out.writeByte(type);
            out.writeShort(len);
            out.write(buf, 0, len);
            out.flush();
        }
    }

    static void sendEvent(DataOutputStream out, String text) {
        try {
            byte[] b = text.getBytes("UTF-8");
            sendFrame(out, T_EVENT, b, b.length);
        } catch (IOException ignored) {}
        log("event: " + text);
    }

    /** Pushes precise call-state changes (dialing/ringing/active/ended) to the computer as events. */
    static final class CallStateListener extends android.telephony.PhoneStateListener {
        final DataOutputStream out;
        final int[] fg;
        CallStateListener(java.util.concurrent.Executor ex, DataOutputStream out, int[] fg) {
            super(ex);
            this.out = out;
            this.fg = fg;
        }
        // Overrides the hidden PhoneStateListener.onPreciseCallStateChanged (see stubs/).
        public void onPreciseCallStateChanged(android.telephony.PreciseCallState s) {
            int f = s.getForegroundCallState(), r = s.getRingingCallState();
            sendEvent(out, "CALL fg=" + f + " ring=" + r + " bg=" + s.getBackgroundCallState());
            synchronized (fg) { fg[0] = f; fg[1] = r; fg.notifyAll(); }
        }
    }

    /**
     * Bridge session: started BEFORE the call is placed/answered so nothing has to boot when the
     * other side picks up. Reports call state, then opens the interception audio paths as soon as
     * the call is active and streams until the computer sends STOP or disconnects.
     */
    static void bridge(AudioManager am, int upRate, boolean withAudio) throws Exception {
        LocalServerSocket server = new LocalServerSocket("callaudio");
        log("LISTENING");
        LocalSocket sock = server.accept();
        server.close();
        DataInputStream in = new DataInputStream(new BufferedInputStream(sock.getInputStream(), 16384));
        DataOutputStream out = new DataOutputStream(new BufferedOutputStream(sock.getOutputStream(), 4096));
        log("client connected, audio mode=" + modeName(am.getMode()));

        final int[] fg = {-1, -1};  // [foreground state, ringing state]
        try {
            Constructor<android.telephony.TelephonyManager> tmc =
                    android.telephony.TelephonyManager.class.getDeclaredConstructor(Context.class);
            tmc.setAccessible(true);
            android.telephony.TelephonyManager tm = tmc.newInstance(fakeContext);
            tm.listen(new CallStateListener(java.util.concurrent.Executors.newSingleThreadExecutor(), out, fg),
                    0x00000800 /* PhoneStateListener.LISTEN_PRECISE_CALL_STATE */);
            sendEvent(out, "LISTENER ok");
        } catch (Throwable t) {
            sendEvent(out, "LISTENER failed " + root(t));
        }

        final AudioFormat inFmt = new AudioFormat.Builder().setSampleRate(RATE)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_IN_MONO).build();
        final AudioFormat outFmt = new AudioFormat.Builder().setSampleRate(upRate)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build();
        final AudioRecord[] recH = {null};
        final AudioTrack[] trackH = {null};
        final boolean[] running = {true};

        // Audio thread: wait for the call to become active, open interception, stream downlink.
        Thread audio = new Thread(() -> {
            try {
                synchronized (fg) {
                    while (running[0] && fg[0] != 1) fg.wait(200);
                }
                if (!running[0]) return;
                long t0 = System.currentTimeMillis();
                String lastErr = "";
                while (running[0] && (recH[0] == null || trackH[0] == null)) {
                    try {
                        if (recH[0] == null) recH[0] = (AudioRecord) call(am, "getCallDownlinkExtractionAudioRecord", new Class<?>[]{AudioFormat.class}, inFmt);
                        if (trackH[0] == null) trackH[0] = (AudioTrack) call(am, "getCallUplinkInjectionAudioTrack", new Class<?>[]{AudioFormat.class}, outFmt);
                    } catch (Throwable t) {
                        lastErr = String.valueOf(root(t));
                        if (System.currentTimeMillis() - t0 > 10000) { sendEvent(out, "ERROR " + lastErr); return; }
                        Thread.sleep(100);
                    }
                }
                recH[0].startRecording();
                trackH[0].play();
                sendEvent(out, "READY upRate=" + upRate + " openMs=" + (System.currentTimeMillis() - t0)
                        + " trackBufferFrames=" + trackH[0].getBufferSizeInFrames());
                byte[] buf = new byte[RATE / 50 * 2];  // 20 ms
                while (running[0]) {
                    int n = recH[0].read(buf, 0, buf.length);
                    if (n < 0) { sendEvent(out, "ERROR downlink read " + n); break; }
                    if (n > 0) sendFrame(out, T_AUDIO, buf, n);
                }
            } catch (InterruptedException ignored) {
            } catch (IOException e) {
                log("downlink socket closed: " + e);
            } catch (Throwable t) {
                sendEvent(out, "ERROR audio thread " + root(t));
            }
        });
        // In "events" mode the call audio goes over Bluetooth instead; we only report call state.
        if (withAudio) audio.start();
        else sendEvent(out, "EVENTS_ONLY");

        // Uplink: computer -> call (audio frames arriving before READY are dropped).
        boolean micMuted = false;
        byte[] payload = new byte[65535];
        try {
            while (true) {
                int type = in.readUnsignedByte();
                int len = in.readUnsignedShort();
                in.readFully(payload, 0, len);
                AudioTrack tr = trackH[0];
                if (type == T_AUDIO) {
                    if (tr != null) tr.write(payload, 0, len);
                } else if (type == T_FLUSH) {
                    if (tr != null) { tr.pause(); tr.flush(); tr.play(); }
                } else if (type == T_MUTE && len >= 1) {
                    micMuted = payload[0] != 0;
                    am.setMicrophoneMute(micMuted);
                    sendEvent(out, "MIC " + (am.isMicrophoneMute() ? "muted" : "live"));
                } else if (type == T_STOP) {
                    break;
                }
            }
        } catch (EOFException e) {
            log("computer disconnected");
        } catch (IOException e) {
            log("uplink socket error: " + e);
        }

        running[0] = false;
        if (micMuted) am.setMicrophoneMute(false);
        if (withAudio) audio.join(1500);
        if (recH[0] != null) { try { recH[0].stop(); } catch (Throwable ignored) {} recH[0].release(); }
        if (trackH[0] != null) { try { trackH[0].stop(); } catch (Throwable ignored) {} trackH[0].release(); }
        try { sock.close(); } catch (IOException ignored) {}
        log("bridge stopped");
    }

    /** Records `seconds` from an AudioRecord into a 16 kHz mono WAV, logging RMS once per second. */
    static void capture(AudioRecord rec, File out, int seconds, String label) throws Exception {
        try (FileOutputStream fos = new FileOutputStream(out)) {
            fos.write(new byte[44]);  // header placeholder
            rec.startRecording();
            log(label + " recording state=" + rec.getRecordingState());
            short[] buf = new short[RATE / 10];  // 100 ms
            byte[] bytes = new byte[buf.length * 2];
            long total = 0;
            double secSq = 0; int secN = 0; int sec = 0;
            long end = System.currentTimeMillis() + seconds * 1000L;
            while (System.currentTimeMillis() < end) {
                int n = rec.read(buf, 0, buf.length);
                if (n <= 0) { log(label + " read returned " + n); Thread.sleep(50); continue; }
                for (int i = 0; i < n; i++) {
                    secSq += buf[i] * (double) buf[i];
                    bytes[2 * i] = (byte) buf[i];
                    bytes[2 * i + 1] = (byte) (buf[i] >> 8);
                }
                fos.write(bytes, 0, n * 2);
                total += n; secN += n;
                if (secN >= RATE) {
                    log(String.format("%s t=%ds rms=%.0f", label, ++sec, Math.sqrt(secSq / secN)));
                    secSq = 0; secN = 0;
                }
            }
            rec.stop();
            log(label + ": captured " + total + " samples");
        }
        writeWavHeader(out, RATE);
    }

    static void writeWavHeader(File f, int rate) throws IOException {
        try (RandomAccessFile raf = new RandomAccessFile(f, "rw")) {
            long dataLen = raf.length() - 44;
            raf.seek(0);
            raf.writeBytes("RIFF"); writeLE(raf, (int) (dataLen + 36), 4); raf.writeBytes("WAVE");
            raf.writeBytes("fmt "); writeLE(raf, 16, 4); writeLE(raf, 1, 2); writeLE(raf, 1, 2);
            writeLE(raf, rate, 4); writeLE(raf, rate * 2, 4); writeLE(raf, 2, 2); writeLE(raf, 16, 2);
            raf.writeBytes("data"); writeLE(raf, (int) dataLen, 4);
        }
    }

    static void writeLE(RandomAccessFile raf, int v, int bytes) throws IOException {
        for (int i = 0; i < bytes; i++) raf.write((v >> (8 * i)) & 0xff);
    }

    public static void main(String[] args) throws Exception {
        Looper.prepareMainLooper();
        setupContext();
        AudioManager am = audioManager();
        String mode = args.length > 0 ? args[0] : "probe";
        Thread worker = new Thread(() -> {
            int code = 0;
            try {
                if (mode.equals("bridge")) {
                    boolean withAudio = !(args.length > 2 && args[2].equals("events"));
                    bridge(am, args.length > 1 ? Integer.parseInt(args[1]) : 24000, withAudio);
                } else if (mode.equals("test")) {
                    int secs = args.length > 1 ? Integer.parseInt(args[1]) : 10;
                    String wav = args.length > 2 && !args[2].equals("-") ? args[2] : null;
                    boolean redirect = args.length > 3 && args[3].equals("redirect");
                    test(am, secs, wav, redirect);
                } else {
                    probe(am);
                }
            } catch (Throwable t) {
                log("FATAL: " + root(t));
                t.printStackTrace();
                code = 1;
            }
            System.exit(code);
        });
        worker.start();
        Looper.loop();  // service callbacks (e.g. audio policy) are delivered on the main looper
    }
}
