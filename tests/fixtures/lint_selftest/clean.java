/**
 * Clean reference fixture for Layer A self-test.
 *
 * Every parameter has a unit literal, every quantity has a leading
 * value, every .set() call has a non-placeholder description. No A1–A6
 * findings expected; no missing/placeholder descriptions expected.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

import java.util.Map;

public class CleanBuilder {

    public static Model buildModel(Map<String,String> args) {
        Model model = ModelUtil.create("Clean");

        // TIER A: parameters with units and descriptions.
        model.param().set("L", "1[m]", "Side length");
        model.param().set("rho", "8960[kg/m^3]", "Density");
        model.param().set("cp", "385[J/(kg*K)]", "Heat capacity");
        model.param().set("T0", "293.15[K]", "Reference temperature");

        // TIER A: component variables.
        model.component().create("comp1", true);
        model.component("comp1").variable().create("var1");
        model.component("comp1").variable("var1").set(
            "Pv", "rho*cp*T0", "Volumetric energy density"
        );

        model.component("comp1").geom().create("geom1", 3);
        model.component("comp1").geom("geom1").run();
        return model;
    }
}
