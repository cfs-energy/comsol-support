/**
 * Minimal buildModel contract example — single 3D box, no mesh, no
 * physics, no study. Used by mphgen E2E test.
 *
 * Accepts one arg: size_m (default 0.01). The box is size_m on a side.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

import java.util.Map;

public class SimpleBoxBuilder {

    public static Model buildModel(Map<String,String> args) {
        double size = 0.01;
        if (args != null && args.containsKey("size_m")) {
            try { size = Double.parseDouble(args.get("size_m")); }
            catch (NumberFormatException e) { /* keep default */ }
        }

        Model model = ModelUtil.create("SimpleBox");
        model.param().set("L", size + "[m]");
        model.component().create("comp1", true);
        model.component("comp1").geom().create("geom1", 3);
        model.component("comp1").geom("geom1").create("blk1", "Block");
        model.component("comp1").geom("geom1").feature("blk1")
            .set("size", new String[]{"L", "L", "L"});
        model.component("comp1").geom("geom1").run();
        return model;
    }
}
