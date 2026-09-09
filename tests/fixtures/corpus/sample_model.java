/*
 * Sample COMSOL-generated Java model file.
 * Exercises geometry, physics, materials, mesh, study, and postprocessing patterns.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

public class sample_model {

    public static void main(String[] args) {
        Model model = ModelUtil.create("Model");

        // Parameters
        model.param().set("L", "0.1[m]", "Length");
        model.param().set("W", "0.05[m]", "Width");
        model.param().set("H", "0.01[m]", "Height");

        // Component
        model.component().create("comp1", true);

        // Functions
        model.component("comp1").func().create("an1", "Analytic");
        model.component("comp1").func("an1").set("funcname", "T_init");
        model.component("comp1").func("an1").set("expr", "293.15+100*x");

        // Geometry
        model.component("comp1").geom().create("geom1", 3);
        model.component("comp1").geom("geom1").create("blk1", "Block");
        model.component("comp1").geom("geom1").feature("blk1").set("size", new double[]{0.1, 0.05, 0.01});
        model.component("comp1").geom("geom1").feature("blk1").set("pos", new double[]{0, 0, 0});
        model.component("comp1").geom("geom1").create("cyl1", "Cylinder");
        model.component("comp1").geom("geom1").feature("cyl1").set("r", "0.005");
        model.component("comp1").geom("geom1").feature("cyl1").set("h", "0.01");
        model.component("comp1").geom("geom1").feature("cyl1").set("pos", new double[]{0.05, 0.025, 0});
        model.component("comp1").geom("geom1").create("dif1", "Difference");
        model.component("comp1").geom("geom1").feature("dif1").selection("input").set("blk1");
        model.component("comp1").geom("geom1").feature("dif1").selection("input2").set("cyl1");
        model.component("comp1").geom("geom1").run("fin");

        // Selections
        model.component("comp1").selection().create("sel1", "Explicit");
        model.component("comp1").selection("sel1").set("entitydim", "2");

        // Materials
        model.component("comp1").material().create("mat1", "Common");
        model.component("comp1").material("mat1").propertyGroup("def").set("thermalconductivity", "400[W/(m*K)]");
        model.component("comp1").material("mat1").propertyGroup("def").set("density", "8960[kg/m^3]");

        // Physics
        model.component("comp1").physics().create("ht", "HeatTransfer", "geom1");
        model.component("comp1").physics("ht").create("temp1", "TemperatureBoundary");
        model.component("comp1").physics("ht").feature("temp1").set("T0", "373.15[K]");
        model.component("comp1").physics("ht").create("hf1", "HeatFluxBoundary");
        model.component("comp1").physics("ht").feature("hf1").set("q0", "1000[W/m^2]");

        // Mesh
        model.component("comp1").mesh().create("mesh1");
        model.component("comp1").mesh("mesh1").create("ftet1", "FreeTet");
        model.component("comp1").mesh("mesh1").feature("ftet1").create("size1", "Size");
        model.component("comp1").mesh("mesh1").feature("ftet1").feature("size1").set("hmax", "0.005");

        // Study
        model.study().create("std1");
        model.study("std1").create("stat", "Stationary");

        // Results
        model.result().create("pg1", "PlotGroup3D");
        model.result("pg1").create("surf1", "Surface");
        model.result("pg1").feature("surf1").set("expr", "T");
        model.result().numerical().create("gev1", "EvalGlobal");
        model.result().numerical("gev1").set("expr", "T_avg");
    }
}
