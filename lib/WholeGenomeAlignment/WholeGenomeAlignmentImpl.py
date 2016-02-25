#BEGIN_HEADER
import os
import sys
import traceback
import json
import logging
import subprocess
import tempfile
import uuid
import hashlib

from datetime import datetime

from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord

from biokbase.workspace.client import Workspace as workspaceService


logging.basicConfig(format="[%(asctime)s %(levelname)s %(name)s] %(message)s",
                    level=logging.INFO)
logger = logging.getLogger(__name__)

#END_HEADER


class WholeGenomeAlignment:
    '''
    Module Name:
    WholeGenomeAlignment

    Module Description:
    A KBase module: WholeGenomeAlignment
    '''

    ######## WARNING FOR GEVENT USERS #######
    # Since asynchronous IO can lead to methods - even the same method -
    # interrupting each other, you must be *very* careful when using global
    # state. A method could easily clobber the state set by another while
    # the latter method is running.
    #########################################
    #BEGIN_CLASS_HEADER
    workspaceURL = None

    # target is a list for collecting log messages
    def log(self, target, message):
        # we should do something better here...
        if target is not None:
            target.append(message)
        print(message)
        sys.stdout.flush()
        # logger.debug(message)

    def contigset_to_fasta(self, contigset, fasta_file):
        records = []
        for contig in contigset['contigs']:
            record = SeqRecord(Seq(contig['sequence']), id=contig['id'], description='')
            records.append(record)
        SeqIO.write(records, fasta_file, "fasta")

    def create_temp_json(self, attrs):
        f = tempfile.NamedTemporaryFile(delete=False)
        outjson = f.name
        f.write(json.dumps(attrs))
        f.close()
        return outjson
    #END_CLASS_HEADER

    # config contains contents of config file in a hash or None if it couldn't
    # be found
    def __init__(self, config):
        #BEGIN_CONSTRUCTOR
        self.workspaceURL = config['workspace-url']
        self.scratch = os.path.abspath(config['scratch'])
        if not os.path.exists(self.scratch):
            os.makedirs(self.scratch)
        #END_CONSTRUCTOR
        pass

    def run_mugsy(self, ctx, params):
        # ctx is the context object
        # return variables are: output
        #BEGIN run_mugsy

        logger.info("Running Mugsy with params = {}".format(json.dumps(params)))

        token = ctx["token"]
        ws = workspaceService(self.workspaceURL, token=token)
        wsid = None

        genomeset = None
        if "input_genomeset_ref" in params and params["input_genomeset_ref"] is not None:
            logger.info("Loading GenomeSet object from workspace")
            objects = ws.get_objects([{"ref": params["input_genomeset_ref"]}])
            genomeset = objects[0]["data"]
            wsid = objects[0]['info'][6]

        genome_refs = []
        if genomeset is not None:
            for param_key in genomeset["elements"]:
                genome_refs.append(genomeset["elements"][param_key]["ref"])
            logger.info("Genome references from genome set: {}".format(genome_refs))

        if "input_genome_refs" in params and params["input_genome_refs"] is not None:
            for genome_ref in params["input_genome_refs"]:
                if genome_ref is not None:
                    genome_refs.append(genome_ref)

        logger.info("Final list of genome references: {}".format(genome_refs))
        if len(genome_refs) < 2:
            raise ValueError("Number of genomes should be more than 1")
        if len(genome_refs) > 10:
            raise ValueError("Number of genomes exceeds 10, which is too many for mugsy")

        timestamp = int((datetime.utcnow() - datetime.utcfromtimestamp(0)).total_seconds()*1000)
        output_dir = os.path.join(self.scratch, 'output.'+str(timestamp))
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        genome_names = []
        fasta_files = []
        for pos, ref in enumerate(genome_refs):
            logger.info("Loading Genome object from workspace for ref: ".format(ref))

            obj = ws.get_objects([{"ref": ref}])[0]
            data = obj["data"]
            info = obj["info"]
            wsid = wsid or info[6]
            type_name = info[2].split('.')[1].split('-')[0]
            logger.info("type_name = {}".format(type_name))

            # if KBaseGenomes.ContigSet
            if type_name == 'Genome':
                # logger.debug("genome = {}".format(json.dumps(data)))
                genome_names.append(data.get("scientific_name", "") + " ({})".format(ref))
                contigset_ref = data["contigset_ref"]
                obj = ws.get_objects([{"ref": contigset_ref}])[0]
                data = obj["data"]
                info = obj["info"]
                # logger.debug("data = {}".format(json.dumps(data)))
            else:
                genome_names.append(ref.split('/')[1] + " ({})".format(ref.split('/')[0]))


            fasta_name = os.path.join(output_dir, "{}.fa".format(pos+1))
            self.contigset_to_fasta(data, fasta_name)
            fasta_files.append(fasta_name)

            # data_ref = str(info[6]) + "/" + str(info[0]) + "/" + str(info[4])

            # logger.info("info = {}".format(json.dumps(info)))
            # logger.info("data = <<<<<<{}>>>>>>".format(json.dumps(data)))

        logger.info("fasta_files = {}".format(fasta_files))

        logger.info("Run Mugsy:")

        cmd = ['mugsy', '-p', 'out', '--directory', output_dir ]

        if 'minlength' in params:
            if params['minlength']:
                cmd.append('--minlength')
                cmd.append(str(params['minlength']))
        if 'distance' in params:
            if params['distance']:
                cmd.append('--distance')
                cmd.append(str(params['distance']))

        cmd += fasta_files

        logger.info("CMD: {}".format(' '.join(cmd)))
        p = subprocess.Popen(cmd,
                             cwd = self.scratch,
                             stdout = subprocess.PIPE,
                             stderr = subprocess.STDOUT, shell = False)

        console = []
        while True:
            line = p.stdout.readline()
            if not line: break
            self.log(console, line.replace('\n', ''))

        p.stdout.close()
        p.wait()
        logger.debug('return code: {}'.format(p.returncode))
        if p.returncode != 0:
            raise ValueError('Error running mugsy, return code: {}\n\n{}'.format(p.returncode, '\n'.join(console)))


        report = 'Genomes/ContigSets aligned with Mugsy:\n'
        for pos, name in enumerate(genome_names):
            report += '  {}: {}\n'.format(pos+1, name)

        report += '\n\n============= MAF output =============\n\n'
        maf_file = os.path.join(output_dir, 'out.maf')
        with open(maf_file, 'r') as f:
            for line in f:
                line = line.replace('\n', '')
                if len(line) > 80:
                    report += line[:80]+"...\n"
                else:
                    report += line+"\n"

        print(report)

        aln_fasta = os.path.join(output_dir, 'aln.fasta')
        cmdstr = 'maf2fasta.pl < {} | sed "s/=//g" > {}'.format(maf_file, aln_fasta)
        logger.debug('CMD: {}'.format(cmdstr))
        subprocess.check_call(cmdstr, shell=True)

        # Warning: this reads everything into memory!  Will not work if
        # the contigset is very large!
        contigset_data = {
            'id': 'mugsy.aln',
            'source': 'User assembled contigs from reads in KBase',
            'source_id':'none',
            'md5': 'md5 of what? concat seq? concat md5s?',
            'contigs':[]
        }

        lengths = []
        for seq_record in SeqIO.parse(aln_fasta, 'fasta'):
            contig = {
                'id': seq_record.id,
                'name': seq_record.name,
                'description': seq_record.description,
                'length': len(seq_record.seq),
                'sequence': str(seq_record.seq),
                'md5': hashlib.md5(str(seq_record.seq)).hexdigest()
            }
            lengths.append(contig['length'])
            contigset_data['contigs'].append(contig)


        # provenance
        input_ws_objects = []
        if "input_genomeset_ref" in params and params["input_genomeset_ref"] is not None:
            input_ws_objects.append(params["input_genomeset_ref"])
        if "input_genome_refs" in params and params["input_genome_refs"] is not None:
            for genome_ref in params["input_genome_refs"]:
                if genome_ref is not None:
                    input_ws_objects.append(genome_ref)

        provenance = None
        if "provenance" in ctx:
            provenance = ctx["provenance"]
        else:
            logger.info("Creating provenance data")
            provenance = [{"service": "WholeGenomeAlignment",
                           "method": "run_mugsy",
                           "method_params": [params]}]

        provenance[0]["input_ws_objects"] = input_ws_objects
        provenance[0]["description"] = "whole genome alignment using mugsy"


        # save the alignment object
        aln_obj_info = ws.save_objects({
            'id': wsid, # set the output workspace ID
            'objects':[{'type': 'ComparativeGenomics.WholeGenomeAlignment',
                        'data': contigset_data,
                        'name': params['output_alignment_name'],
                        'meta': {},
                        'provenance': provenance}]})


        reportObj = {
            'objects_created':[{'ref':params['workspace_name']+'/'+params['output_alignment_name'], 'description':'Mugsy whole genome alignment'}],
            'text_message': report
        }

        reportName = '{}.report.{}'.format('run_mugsy', hex(uuid.getnode()))
        report_obj_info = ws.save_objects({
                # 'workspace': params["workspace_name"],
            'id': wsid,
            'objects': [
                {
                    'type': 'KBaseReport.Report',
                    'data': reportObj,
                    'name': reportName,
                    'meta': {},
                    'hidden': 1,
                    'provenance': provenance
                }
            ]})[0]


        # shutil.rmtree(output_dir)

        output = {"report_name": reportName, 'report_ref': str(report_obj_info[6]) + '/' + str(report_obj_info[0]) + '/' + str(report_obj_info[4]) }

        #END run_mugsy

        # At some point might do deeper type checking...
        if not isinstance(output, dict):
            raise ValueError('Method run_mugsy return value ' +
                             'output is not type dict as required.')
        # return the results
        return [output]



    def run_mauve(self, ctx, params):
        # ctx is the context object
        # return variables are: output
        #BEGIN run_mauve

        logger.info("Running progressiveMauve with params = {}".format(json.dumps(params)))

        token = ctx["token"]
        ws = workspaceService(self.workspaceURL, token=token)
        wsid = None

        genomeset = None
        if "input_genomeset_ref" in params and params["input_genomeset_ref"] is not None:
            logger.info("Loading GenomeSet object from workspace")
            objects = ws.get_objects([{"ref": params["input_genomeset_ref"]}])
            genomeset = objects[0]["data"]
            wsid = objects[0]['info'][6]

        genome_refs = []
        if genomeset is not None:
            for param_key in genomeset["elements"]:
                genome_refs.append(genomeset["elements"][param_key]["ref"])
            logger.info("Genome references from genome set: {}".format(genome_refs))

        if "input_genome_refs" in params and params["input_genome_refs"] is not None:
            for genome_ref in params["input_genome_refs"]:
                if genome_ref is not None:
                    genome_refs.append(genome_ref)

        logger.info("Final list of genome references: {}".format(genome_refs))
        if len(genome_refs) < 2:
            raise ValueError("Number of genomes should be more than 1")
        if len(genome_refs) > 10:
            raise ValueError("Number of genomes exceeds 10, which is too many for mauve")

        timestamp = int((datetime.utcnow() - datetime.utcfromtimestamp(0)).total_seconds()*1000)
        output_dir = os.path.join(self.scratch, 'output.'+str(timestamp))
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        genome_names = []
        fasta_files = []
        for pos, ref in enumerate(genome_refs):
            logger.info("Loading Genome object from workspace for ref: ".format(ref))

            obj = ws.get_objects([{"ref": ref}])[0]
            data = obj["data"]
            info = obj["info"]
            wsid = wsid or info[6]
            type_name = info[2].split('.')[1].split('-')[0]
            logger.info("type_name = {}".format(type_name))

            # if KBaseGenomes.ContigSet
            if type_name == 'Genome':
                # logger.debug("genome = {}".format(json.dumps(data)))
                genome_names.append(data.get("scientific_name", "") + " ({})".format(ref))
                contigset_ref = data["contigset_ref"]
                obj = ws.get_objects([{"ref": contigset_ref}])[0]
                data = obj["data"]
                info = obj["info"]
                # logger.debug("data = {}".format(json.dumps(data)))
            else:
                genome_names.append(ref.split('/')[1] + " ({})".format(ref.split('/')[0]))


            fasta_name = os.path.join(output_dir, "{}.fa".format(pos+1))
            self.contigset_to_fasta(data, fasta_name)
            fasta_files.append(fasta_name)

            # data_ref = str(info[6]) + "/" + str(info[0]) + "/" + str(info[4])

            # logger.info("info = {}".format(json.dumps(info)))
            # logger.info("data = <<<<<<{}>>>>>>".format(json.dumps(data)))

        logger.info("fasta_files = {}".format(fasta_files))

        logger.info("Run progressiveMauve:")

        xmfa_file = os.path.join(output_dir, 'out.xmfa')

        cmd = ['progressiveMauve', '--output={}'.format(xmfa_file)]

        if 'max_breakpoint_distance_scale' in params:
            if params['max_breakpoint_distance_scale']:
                cmd.append('--max-breakpoint-distance-scale')
                cmd.append(str(params['max_breakpoint_distance_scale']))
        if 'conservation_distance_scale' in params:
            if params['conservation_distance_scale']:
                cmd.append('--conservation-distance-scale')
                cmd.append(str(params['conservation_distance_scale']))
        if 'hmm_identity' in params:
            if params['hmm_identity']:
                cmd.append('--hmm-identity')
                cmd.append(str(params['hmm_identity']))

        cmd += fasta_files

        logger.info("CMD: {}".format(' '.join(cmd)))
        p = subprocess.Popen(cmd,
                             cwd = self.scratch,
                             stdout = subprocess.PIPE,
                             stderr = subprocess.STDOUT, shell = False)

        console = []
        while True:
            line = p.stdout.readline()
            if not line: break
            self.log(console, line.replace('\n', ''))

        p.stdout.close()
        p.wait()
        logger.debug('return code: {}'.format(p.returncode))
        if p.returncode != 0:
            raise ValueError('Error running progressiveMauve, return code: {}\n\n{}'.format(p.returncode, '\n'.join(console)))


        report = 'Genomes/ContigSets aligned with Mauve:\n'
        for pos, name in enumerate(genome_names):
            report += '  {}: {}\n'.format(pos+1, name)

        report += '\n\n============= XMFA.backbone output =============\n\n'
        backbone_file =  os.path.join(output_dir, 'out.xmfa.backbone')
        with open(backbone_file, 'r') as f:
            for line in f:
                line = line.replace('\n', '')
                if len(line) > 80:
                    report += line[:80]+"...\n"
                else:
                    report += line+"\n"

        print(report)

        aln_fasta = os.path.join(output_dir, 'aln.fasta')
        cmdstr = 'cat {} | sed "s/^#.*//g; s/=//g" > {}'.format(xmfa_file, aln_fasta)
        logger.debug('CMD: {}'.format(cmdstr))
        subprocess.check_call(cmdstr, shell=True)

        # Warning: this reads everything into memory!  Will not work if
        # the contigset is very large!
        contigset_data = {
            'id': 'mauve.aln',
            'source': 'User assembled contigs from reads in KBase',
            'source_id':'none',
            'md5': 'md5 of what? concat seq? concat md5s?',
            'contigs':[]
        }

        lengths = []
        for seq_record in SeqIO.parse(aln_fasta, 'fasta'):
            contig = {
                'id': seq_record.id,
                'name': seq_record.name,
                'description': seq_record.description,
                'length': len(seq_record.seq),
                'sequence': str(seq_record.seq),
                'md5': hashlib.md5(str(seq_record.seq)).hexdigest()
            }
            lengths.append(contig['length'])
            contigset_data['contigs'].append(contig)


        # provenance
        input_ws_objects = []
        if "input_genomeset_ref" in params and params["input_genomeset_ref"] is not None:
            input_ws_objects.append(params["input_genomeset_ref"])
        if "input_genome_refs" in params and params["input_genome_refs"] is not None:
            for genome_ref in params["input_genome_refs"]:
                if genome_ref is not None:
                    input_ws_objects.append(genome_ref)

        provenance = None
        if "provenance" in ctx:
            provenance = ctx["provenance"]
        else:
            logger.info("Creating provenance data")
            provenance = [{"service": "WholeGenomeAlignment",
                           "method": "run_mauve",
                           "method_params": [params]}]

        provenance[0]["input_ws_objects"] = input_ws_objects
        provenance[0]["description"] = "whole genome alignment using mauve"


        # save the alignment object
        aln_obj_info = ws.save_objects({
            'id': wsid, # set the output workspace ID
            'objects':[{'type': 'ComparativeGenomics.WholeGenomeAlignment',
                        'data': contigset_data,
                        'name': params['output_alignment_name'],
                        'meta': {},
                        'provenance': provenance}]})


        reportObj = {
            'objects_created':[{'ref':params['workspace_name']+'/'+params['output_alignment_name'], 'description':'Mauve whole genome alignment'}],
            'text_message': report
        }

        reportName = '{}.report.{}'.format('run_mauve', hex(uuid.getnode()))
        report_obj_info = ws.save_objects({
                # 'workspace': params["workspace_name"],
            'id': wsid,
            'objects': [
                {
                    'type': 'KBaseReport.Report',
                    'data': reportObj,
                    'name': reportName,
                    'meta': {},
                    'hidden': 1,
                    'provenance': provenance
                }
            ]})[0]


        # shutil.rmtree(output_dir)

        output = {"report_name": reportName, 'report_ref': str(report_obj_info[6]) + '/' + str(report_obj_info[0]) + '/' + str(report_obj_info[4]) }

        #END run_mauve

        # At some point might do deeper type checking...
        if not isinstance(output, dict):
            raise ValueError('Method run_mauve return value ' +
                             'output is not type dict as required.')
        # return the results
        return [output]
