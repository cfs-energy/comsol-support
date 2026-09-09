/**
 * LicenseProbe — bounded check for COMSOL license-seat availability.
 *
 * The bundled `lmutil lmstat` cannot query every FlexNet server (version
 * mismatch; -12/-96 measured), so seat usage can't be inspected directly.
 * The robust alternative is to *attempt* a checkout. Measured on 6.4:
 * ModelUtil.initStandalone succeeds with NO free seat — the base
 * "COMSOL Multiphysics" seat is only taken at ModelUtil.load — so the
 * probe explicitly checks out the base feature afterwards with
 * ModelUtil.checkoutLicense("COMSOL"); false means every seat is in use.
 * The wall-clock bound in the Python `license-status` command (kills the
 * process group on timeout) still covers a server that blocks.
 *
 * Prints one JSON line:
 *   {"available":true,"checkout_ms":<n>}                      exit 0
 *   {"available":false,"reason":"no_seat","error":"..."}      exit 1
 *   {"available":false,"error":"..."}                         exit 1 (init failed)
 * A blocked probe never reaches the print — the caller's timeout
 * classifies that as unavailable. Model- and module-agnostic.
 *
 * Usage: java LicenseProbe [FEATURE]   (default FEATURE = COMSOL, the base product;
 *        any FlexNet feature name works, e.g. STRUCTURALMECHANICS)
 */

import com.comsol.model.util.*;

public class LicenseProbe {
    public static void main(String[] args) {
        String feature = args.length > 0 ? args[0] : "COMSOL";
        long t0 = System.currentTimeMillis();
        try {
            ModelUtil.initStandalone(false);
        } catch (Throwable t) {
            String msg = t.getMessage() != null
                    ? t.getMessage() : t.getClass().getName();
            System.out.println("{\"available\":false,\"error\":\""
                    + msg.replace("\\", "\\\\").replace("\"", "\\\"")
                    + "\"}");
            System.out.flush();
            System.exit(1);
        }
        // initStandalone alone is not evidence of a seat (measured: it
        // returned in ~2 s while every model load failed for want of one).
        boolean seat;
        try {
            seat = ModelUtil.checkoutLicense(feature);
        } catch (Throwable t) {
            String msg = t.getMessage() != null
                    ? t.getMessage() : t.getClass().getName();
            System.out.println("{\"available\":false,\"error\":\""
                    + msg.replace("\\", "\\\\").replace("\"", "\\\"")
                    + "\"}");
            System.out.flush();
            System.exit(1);
            return;
        }
        long ms = System.currentTimeMillis() - t0;
        if (!seat) {
            System.out.println("{\"available\":false,\"reason\":\"no_seat\","
                    + "\"error\":\"no free seat for " + feature
                    + " (ModelUtil.checkoutLicense returned false)\","
                    + "\"checkout_ms\":" + ms + "}");
            System.out.flush();
            System.exit(1);
            return;
        }
        System.out.println("{\"available\":true,\"checkout_ms\":" + ms + "}");
        System.out.flush();
        // Clean exit closes the TCP connection so the seat is released
        // immediately (see G-DISCONNECT-HANGS — we never call disconnect).
        System.exit(0);
    }
}
