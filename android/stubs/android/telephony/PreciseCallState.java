package android.telephony;

/**
 * Compile-time stub for the hidden/system framework class of the same name. It is NOT dexed:
 * at runtime the phone's real android.telephony.PreciseCallState is used, which lets CallAudio
 * override PhoneStateListener.onPreciseCallStateChanged(PreciseCallState).
 */
public final class PreciseCallState {
    public int getRingingCallState() { throw new RuntimeException("stub"); }
    public int getForegroundCallState() { throw new RuntimeException("stub"); }
    public int getBackgroundCallState() { throw new RuntimeException("stub"); }
}
