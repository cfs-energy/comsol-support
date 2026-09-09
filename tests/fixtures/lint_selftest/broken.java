/**
 * Deliberately-broken fixture for Layer A self-test.
 *
 * Pins exact counts of findings per pattern. If a regex regresses or a
 * suppression rule changes, the test fails. Comments mark which pattern
 * is intended to fire.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

import java.util.Map;

public class BrokenBuilder {

    public static Model buildModel(Map<String,String> args) {
        Model model = ModelUtil.create("Broken");

        // A1: bracket-op-bracket — fires.
        model.param().set("v1", "10[m]/[s]", "Velocity (typo)");

        // A4: addition inside brackets — fires.
        model.param().set("bad_unit_arith", "1[m+s]", "Unit arithmetic typo");

        // A5: non-ASCII inside brackets — fires (Ω, μ, °C all qualify).
        model.param().set("R", "5[Ω]", "Resistance");

        // Missing description (3rd arg absent) — fires once.
        model.param().set("no_descr", "1[A]");

        // Placeholder description ("TBD") — fires once.
        model.param().set("placeholder", "2[V]", "TBD");

        model.component().create("comp1", true);
        model.component("comp1").geom().create("geom1", 3);
        model.component("comp1").geom("geom1").run();
        return model;
    }
}
