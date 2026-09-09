/*
 * Minimal COMSOL model — geometry only, no physics or studies.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

public class minimal_model {

    public static void main(String[] args) {
        Model model = ModelUtil.create("Model");

        model.component().create("comp1", true);
        model.component("comp1").geom().create("geom1", 3);
        model.component("comp1").geom("geom1").create("sph1", "Sphere");
        model.component("comp1").geom("geom1").feature("sph1").set("r", "0.01");
        model.component("comp1").geom("geom1").run("fin");
    }
}
