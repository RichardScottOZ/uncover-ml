#! /usr/bin/env python
"""
Description:
    Gather Metadata for the uncover-ml prediction output results:

Reference: email 2019-05-24
Overview
Creator: (person who generated the model)
Model;
    Name:
    Type and date:
    Algorithm:
    Extent: Lat/long - location on Australia map?

SB Notes: None of the above is required as this information will be captured in the yaml file.

Model inputs:

1.      Covariates - list (in full)
2.      Targets: path to shapefile:  csv file
SB Notes: Only covaraite list file. Targets and path to shapefile is not required as this is available in the yaml file. May be the full path to the shapefile has some merit as one can specify partial path.

Model performance
       JSON file (in full)
SB Notes: Yes

Model outputs

1.      Prediction grid including path
2.      Quantiles Q5; Q95
3.      Variance:
4.      Entropy:
5.      Feature rank file
6.      Raw covariates file (target value - covariate value)
7.      Optimisation output
8.      Others ??
SB Notes: Not required as these are model dependent, and the metadata will be contained in each of the output geotif file.


Model parameters:
1.      YAML file (in full)
2.      .SH file (in full)
SB Notes: The .sh file is not required. YAML file is read as a python dictionary in uncoverml which can be dumped in the metadata.


CreationDate:   31/05/19
Developer:      fei.zhang@ga.gov.au

Revision History:
    LastUpdate:     31/05/19   FZ
    LastUpdate:     dd/mm/yyyy  Who     Optional description
"""

# import section
import os
import sys
import json
import pickle
import datetime
from ppretty import ppretty



class MetadataSummary():
    """
    Summary Description of the ML prediction output
    """

    def __init__(self, model_file):

        path2mf = os.path.dirname(os.path.abspath(model_file))

        print (path2mf)

        self.description = "Metadata for the ML results"
        self.creator = "login_user_fxz547"
        self.datetime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(model_file, 'rb') as f:
            state_dict = pickle.load(f)

        print(state_dict.keys())

        #self.model = state_dict["model"]
        self.config = state_dict["config"]
        self.name = self.config.name  # 'demo_regression'
        self.algorithm = self.config.algorithm  # 'svr'

        self.extent= ((-10, 100),(-40, 140))


        # self.performance_metric= {"json_file": 0.99}
        jsonfilename = "%s_scores.json"%(self.name)

        jsonfile = os.path.join(path2mf, jsonfilename)

        with open(jsonfile) as json_file:
            self.model_performance_metrics = json.load(json_file)


    def write_metadata(self, out_filename):
        with open(out_filename, 'w') as outf:
            outf.write("####### Metadata for the prediction results \n\n")

            objstr = ppretty(self, indent='  ', width=200, seq_length=200,
                             show_protected=True, show_static=True, show_properties=True, show_address=False,
                             str_length=200)

            outf.write(objstr)

            outf.write("\n####### End of Metadata \n")

        return out_filename


def write_prediction_metadata(model_file, out_filename="metadata.txt"):
    """
    write the metadata for this prediction result, into a human-readable YAML file.
    in order to make the ML results traceable and reproduceable (provenance)
    :return:
    """

    from ppretty import ppretty

    with open(model_file, 'rb') as f:
        state_dict = pickle.load(f)

    print(type(state_dict))
    print(state_dict.keys())

    model = state_dict["model"]
    print(type(model))

    # print("####################### -------------------------------  ####################")
    print("####################### wrting the properties of the prediction model  ####################")
    model_str = ppretty(model, indent='  ', width=40, seq_length=10,
                        show_protected=True, show_static=True, show_properties=True, show_address=False, str_length=150)

    config = state_dict["config"]

    # print("#######################  --------------------------------  ####################")
    print("#######################  writing the properties of the config  ####################")

    config_str = ppretty(config, indent='  ', width=200, seq_length=200,
                         show_protected=True, show_static=True, show_properties=True, show_address=False,
                         str_length=200)

    with open(out_filename, 'w') as outf:
        outf.write("####### Metadata for the prediction results ")

        outf.write("\n####### Summary of the ML Result \n")

        outf.write("\n###### Configuration Info \n")
        outf.write(config_str)

        outf.write("\n####### Model Info \n")
        outf.write(model_str)

    return out_filename


def main(mf):
    """
    define my main function
    :return:
    """

    obj= MetadataSummary(mf)
    obj.write_metadata(out_filename='metatest.txt')

    return


# =============================================
# Section for quick test of this script
# ---------------------------------------------
if __name__ == "__main__":
    # call main function
    main(sys.argv[1])
