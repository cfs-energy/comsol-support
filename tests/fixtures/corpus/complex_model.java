/*
 * Complex multi-physics model with multiple studies and geometry operations.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

public class complex_model {

    public static void main(String[] args) {
        Model model = ModelUtil.create("Model");

        // Parameters
        model.param().set("T_hot", "500[K]");
        model.param().set("T_cold", "300[K]");
        model.param().set("P_load", "1e6[Pa]");

        model.component().create("comp1", true);

        // Geometry — boolean operations
        model.component("comp1").geom().create("geom1", 3);
        model.component("comp1").geom("geom1").create("blk1", "Block");
        model.component("comp1").geom("geom1").feature("blk1").set("size", new double[]{0.2, 0.1, 0.05});
        model.component("comp1").geom("geom1").create("blk2", "Block");
        model.component("comp1").geom("geom1").feature("blk2").set("size", new double[]{0.05, 0.05, 0.05});
        model.component("comp1").geom("geom1").feature("blk2").set("pos", new double[]{0.075, 0.025, 0});
        model.component("comp1").geom("geom1").create("cyl1", "Cylinder");
        model.component("comp1").geom("geom1").feature("cyl1").set("r", "0.01");
        model.component("comp1").geom("geom1").feature("cyl1").set("h", "0.05");
        model.component("comp1").geom("geom1").create("uni1", "Union");
        model.component("comp1").geom("geom1").feature("uni1").selection("input").set("blk1", "blk2");
        model.component("comp1").geom("geom1").create("dif1", "Difference");
        model.component("comp1").geom("geom1").feature("dif1").selection("input").set("uni1");
        model.component("comp1").geom("geom1").feature("dif1").selection("input2").set("cyl1");
        model.component("comp1").geom("geom1").create("fil1", "Fillet");
        model.component("comp1").geom("geom1").feature("fil1").set("radius", "0.002");
        model.component("comp1").geom("geom1").run("fin");

        // Selections
        model.component("comp1").selection().create("sel1", "Explicit");
        model.component("comp1").selection("sel1").set("entitydim", "2");
        model.component("comp1").selection().create("sel2", "Explicit");

        // Materials
        model.component("comp1").material().create("mat1", "Common");
        model.component("comp1").material("mat1").propertyGroup("def").set("thermalconductivity", "50");
        model.component("comp1").material("mat1").propertyGroup("def").set("density", "7850");
        model.component("comp1").material("mat1").propertyGroup("def").set("youngsmodulus", "200e9");
        model.component("comp1").material("mat1").propertyGroup("def").set("poissonsratio", "0.3");

        // Physics — Heat Transfer
        model.component("comp1").physics().create("ht", "HeatTransfer", "geom1");
        model.component("comp1").physics("ht").create("temp1", "TemperatureBoundary");
        model.component("comp1").physics("ht").feature("temp1").set("T0", "T_hot");
        model.component("comp1").physics("ht").create("temp2", "TemperatureBoundary");
        model.component("comp1").physics("ht").feature("temp2").set("T0", "T_cold");

        // Physics — Solid Mechanics
        model.component("comp1").physics().create("solid", "SolidMechanics", "geom1");
        model.component("comp1").physics("solid").create("fix1", "Fixed");
        model.component("comp1").physics("solid").create("bndl1", "BoundaryLoad");
        model.component("comp1").physics("solid").feature("bndl1").set("FperArea", new double[]{0, 0, -1e6});

        // Mesh — with boundary layer
        model.component("comp1").mesh().create("mesh1");
        model.component("comp1").mesh("mesh1").create("ftet1", "FreeTet");
        model.component("comp1").mesh("mesh1").create("bl1", "BndLayer");
        model.component("comp1").mesh("mesh1").feature("bl1").create("blp1", "BndLayerProp");
        model.component("comp1").mesh("mesh1").feature("bl1").feature("blp1").set("blnlayers", "3");

        // Study — Stationary thermal + eigenvalue structural
        model.study().create("std1");
        model.study("std1").create("stat", "Stationary");
        model.study("std1").create("eig", "Eigenvalue");
        model.study("std1").feature("eig").set("neigs", "6");
        model.study("std1").feature("eig").setIndex("shift", "100", 0);

        // Results
        model.result().create("pg1", "PlotGroup3D");
        model.result("pg1").create("surf1", "Surface");
        model.result("pg1").feature("surf1").set("expr", "T");
        model.result().create("pg2", "PlotGroup3D");
        model.result("pg2").create("surf2", "Surface");
        model.result("pg2").feature("surf2").set("expr", "solid.mises");
        model.result("pg2").create("def1", "Deformation");
        model.result().numerical().create("gev1", "EvalGlobal");
    }
}
