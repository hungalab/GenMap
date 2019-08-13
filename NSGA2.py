from deap import tools
from deap import base
from deap import algorithms
from deap import creator
import multiprocessing
import numpy
import copy
from time import time
from tqdm import tqdm
from importlib import import_module

from Individual import Individual
from EvalBase import EvalBase
from RouterBase import RouterBase
from Placer import Placer

class NSGA2():
    def __init__(self, config, logfile = None):
        """Constructor of the NSGA2 class.

            Args:
                config (XML Element): a configuration of optimization parameters
                Optional:
                    logfile (_io.TextIOWrapper): log file

            Raise:
                If there exist invalid configurations and parameters, it will raise
                ValueError.
                Also, if router and objectives is not RouterBase and EvalBase class,
                it will raise TypeError.
        """
        # initilize toolbox
        self.__toolbox = base.Toolbox()

        # get parameters
        self.__params = {}
        for param in config.iter("parameter"):
            if "name" in param.attrib:
                if not param.text is None:
                    try:
                        value = int(param.text)
                    except ValueError:
                        try:
                            value = float(param.text)
                        except ValueError:
                            raise ValueError("Invalid parameter: " + param.text)
                    self.__params[param.attrib["name"]] = value
                else:
                    raise ValueError("missing parameter value for " + param.attrib["name"])
            else:
                raise ValueError("missing parameter name")

        # get router
        try:
            router_name = config.find("Router").text
        except AttributeError:
            raise ValueError("missing Router class")
        if router_name is None:
            raise ValueError("Router class name is empty")
        self.__router = getattr(import_module(router_name), router_name)
        if not issubclass(self.__router, RouterBase):
            raise TypeError(self.__router.__name__ + " is not RouterBase class")

        # get objectives
        eval_names = [ele.text for ele in config.iter("eval")]

        if len(eval_names) == 0:
            raise ValueError("At least, one objective is needed")
        if None in eval_names:
            raise ValueError("missing No." + str(eval_names.index(None) + 1) + " objective name")
        self.__eval_list = []
        for eval_name in eval_names:
            evl = getattr(import_module(eval_name), eval_name)
            if not issubclass(evl, EvalBase):
                raise TypeError(evl.__name__ + " is not EvalBase class")
            self.__eval_list.append(evl)

        # get options for each objective
        eval_args_str = [ele.get("args") for ele in config.iter("eval")]
        self.__eval_args = []
        for args in eval_args_str:
            if args is None:
                self.__eval_args.append({})
            else:
                try:
                    args_obj = eval(args)
                except (NameError, SyntaxError) as e:
                    raise ValueError("Invalid arguments for No." + \
                                     str(eval_args_str.index(args) + 1) + " objective")
                if isinstance(args_obj, dict):
                    self.__eval_args.append(args_obj)
                else:
                    raise ValueError("Arguments of evaluation function must be dict: " + str(args_obj))

        self.pop = []
        self.__placer = None
        self.__random_pop_args = []

        # check if hypervolume is available
        self.__hv_logging = True
        try:
            from pygmo.core import hypervolume
            self.hypervolume = hypervolume
        except ImportError:
            self.__hv_logging = False

        # manual ref point
        self.__hv_refpoint = None

        # regist log gile
        self.__logfile = logfile

    def getObjectives(self):
        return self.__eval_list

    def __getstate__(self):
        # make this instance hashable for pickle (needed to use multiprocessing)
        return {"pool": self}


    def setup(self, CGRA, app, sim_params, method, proc_num = multiprocessing.cpu_count()):
        """Setup NSGA2 optimization

            Args:
                CGRA (PEArrayModel): A model of the CGRA
                app (Application): an application to be optimized
                sim_params (SimParameters): a simulation parameters
                method (str): initial mapping method
                    available methods are follows:
                        1. graphviz (default)
                        2. tsort
                        3. random
                Option:
                    proc_num (int): the number of process
                                    Default is equal to cpu count

            Returns:
                bool: if the setup successes, return True, otherwise return False.

        """

        # uni-objective optimization
        if len(self.__eval_list) == 1:
            self.__hv_logging = False

        # initilize weights of network model
        self.__router.set_default_weights(CGRA)

        # obtain CGRA size
        width, height = CGRA.getSize()

        # obrain computation DFG
        comp_dfg = app.getCompSubGraph()

        # generate initial placer
        self.__placer = Placer(method, iterations = self.__params["Initial place iteration"], \
                                randomness = "Full")

        # make initial mappings
        init_maps = self.__placer.generate_init_mappings(comp_dfg, width, height, \
                                                        count = self.__params["Initial place count"],
                                                        proc_num = proc_num)

        self.__random_pop_args = [comp_dfg, width, height, self.__params["Random place count"],\
                                    self.__params["Topological sort probability"]]

        # check pipeline structure
        self.__preg_num = CGRA.getPregNumber()
        self.__pipeline_enable = self.__preg_num > 0

        # check if mapping initialization successed
        if len(init_maps) < 1:
            return False

        # instance setting used in NSGA2
        creator.create("Fitness", base.Fitness, weights=tuple([-1.0 if evl.isMinimize() else 1.0 for evl in self.__eval_list]))
        creator.create("Individual", Individual, fitness=creator.Fitness)

        # setting multiprocessing
        self.__pool = multiprocessing.Pool(proc_num)
        self.__toolbox.register("map", self.__pool.map)

        # register each chromosome operation
        if self.__pipeline_enable > 0:
            self.__toolbox.register("individual", creator.Individual, CGRA, init_maps, self.__preg_num)
        else:
            self.__toolbox.register("individual", creator.Individual, CGRA, init_maps)
        self.__toolbox.register("population", tools.initRepeat, list, self.__toolbox.individual)
        self.__toolbox.register("random_individual", creator.Individual, CGRA)
        self.__toolbox.register("evaluate", self.eval_objectives, self.__eval_list, self.__eval_args, CGRA, app, sim_params, self.__router)
        self.__toolbox.register("mate", Individual.cxSet)
        self.__toolbox.register("mutate", Individual.mutSet, 0.5)
        self.__toolbox.register("select", tools.selNSGA2)

        # set statics method
        self.stats = tools.Statistics(key=lambda ind: ind.fitness.values)
        self.stats.register("min", numpy.min, axis=0)
        self.stats.register("max", numpy.max, axis=0)

         # progress bar
        self.progress = tqdm(total=self.__params["Maximum generation"], dynamic_ncols=True)

        # status display
        self.status_disp = [tqdm(total = 0, dynamic_ncols=True, desc=eval_cls.name(), bar_format="{desc}: {postfix}")\
                            for eval_cls in self.__eval_list]

        return True

    def set_ref_point(self, ref_point):
        """Set manual ref point of hypervolume calculation

            Args:
                ref_point (list): value for each objective

            Returns:
                bool: whether setting is success of not
        """
        if not self.__hv_logging:
            print("Hypervolume logging is disabled")
            print("Ref-point option is ignored")
            return True
        else:
            if len(self.__eval_list) < len(ref_point):
                print("Too few of ref point values")
                return False
            elif len(self.__eval_list) > len(ref_point):
                print("Too much of ref point values")
                return False
            else:
                self.__hv_refpoint = [v for v in ref_point]
                return True

    def random_population(self, n):
        """ Generate rondom mapping as a population.

            Args:
                n (int): the number of the population

            Returns:
                list: a mapping list

        """
        random_mappings = self.__placer.make_random_mappings(*self.__random_pop_args)
        return [self.__toolbox.random_individual(random_mappings, self.__preg_num) for i in range(n)]

    def eval_objectives(self, eval_list, eval_args, CGRA, app, sim_params, router, individual):
        """ Executes evaluation for each objective
        """
        # routing the mapping
        self.__doRouting(CGRA, app, router, individual)
        # evaluate each objectives
        return [eval_cls.eval(CGRA, app, sim_params, individual, **args) \
                for eval_cls, args in zip(eval_list, eval_args)], individual

    def __doRouting(self, CGRA, app, router, individual):
        """
            Execute routing
        """
        # check if routing is necessary
        if individual.isValid():
            return

        # get penalty routing cost
        penalty = router.get_penalty_cost()

        # get a graph which the application to be mapped
        g = individual.routed_graph
        cost = 0
        # comp routing
        cost += router.comp_routing(CGRA, app.getCompSubGraph(), individual.mapping, g)
        if cost > penalty:
            individual.routing_cost = cost + penalty
            return

        # const routing
        cost += router.const_routing(CGRA, app.getConstSubGraph(), individual.mapping, g)
        if cost > penalty:
            individual.routing_cost = cost + penalty
            return

        # input routing
        cost += router.input_routing(CGRA, app.getInputSubGraph(), individual.mapping, g)
        if cost > penalty:
            individual.routing_cost = cost + penalty
            return

        # output routing
        if CGRA.getPregNumber() > 0:
            cost += router.output_routing(CGRA, app.getOutputSubGraph(), \
                                            individual.mapping, g, individual.preg)
        else:
            cost += router.output_routing(CGRA, app.getOutputSubGraph(), individual.mapping, g)

        if cost > penalty:
            individual.routing_cost = cost + penalty
        else:
            # obtain valid routing
            individual.routing_cost = cost
            # eliminate unnecessary nodes and edges
            router.clean_graph(g)
            individual.validate()


    def runOptimization(self):
        # hall of fame
        hof = tools.ParetoFront()

        self.progress.set_description("Initilizing")
        # generate first population
        self.pop = self.__toolbox.population(n=self.__params["Initial population size"])

        # evaluate the population
        fitnesses, self.pop = (list(l) for l in zip(*self.__toolbox.map(self.__toolbox.evaluate, self.pop)))
        for ind, fit in zip(self.pop, fitnesses):
            ind.fitness.values = fit

        # start evolution
        gen_count = 0
        stall_count = 0
        prev_hof = []
        fitness_hof_log = []

        # Repeat evolution
        while gen_count < self.__params["Maximum generation"] and stall_count < self.__params["Maximum stall"]:
            # show generation count
            gen_count = gen_count + 1
            self.progress.set_description("Generation {0}".format(gen_count))
            if not self.__logfile is None:
                self.__logfile.write("Generation {0}\n".format(gen_count))

            # make offspring
            offspring = algorithms.varOr(self.pop, self.__toolbox, self.__params["Offspring size"], \
                                         self.__params["Crossover probability"],\
                                         self.__params["Mutation probability"])

            # Evaluate the individuals of the offspring
            fitnesses, offspring = (list(l) for l in zip(*self.__toolbox.map(self.__toolbox.evaluate, offspring)))
            for ind, fit in zip(offspring, fitnesses):
                ind.fitness.values = fit

            # make next population
            self.pop = self.__toolbox.select(self.pop + offspring , self.__params["Select size"])
            hof.update(self.pop)

            # check if there is an improvement
            if len(hof) == len(prev_hof):
                if set([ind.fitness.values for ind in hof]) == \
                    set([ind.fitness.values for ind in prev_hof]):
                    # no fitness improvement
                    stall_count += 1
                else:
                    stall_count = 0
            else:
                stall_count = 0

            prev_hof = copy.deepcopy(hof)

            # logging hof fitness (only valid individuals)
            fitness_hof_log.append([ind.fitness.values for ind in hof if ind.isValid()])

            # Adding random individuals to the population (attempt to avoid local optimum)
            rnd_ind = self.random_population(self.__params["Random population size"])
            fitnesses, rnd_ind = (list(l) for l in zip(*self.__toolbox.map(self.__toolbox.evaluate, rnd_ind)))
            for ind, fit in zip(rnd_ind, fitnesses):
                ind.fitness.values = fit
            self.pop += rnd_ind

            # update status
            self.progress.set_postfix(hof_len=len(hof), stall=stall_count)
            self.progress.update(1)
            stats = self.stats.compile(hof)
            for i in range(len(stats["min"])):
                self.status_disp[i].set_postfix(min=stats["min"][i], max=stats["max"][i])

            # logging
            if not self.__logfile is None:
                self.__logfile.write("\thof_len = {0} stall = {1}\n".format(len(hof), stall_count))
                for i in range(len(stats["min"])):
                    self.__logfile.write("\t{obj}: min = {min}, max = {max}\n".format(\
                                            obj = self.status_disp[i].desc, min = stats["min"][i],\
                                            max=stats["max"][i]))

        self.__pool.close()
        self.__pool.join()
        if self.__params["Maximum generation"] > gen_count:
            self.progress.update(self.__params["Maximum generation"] - gen_count)
        self.progress.close()
        for disp in self.status_disp:
            disp.close()

        print("\n\nFinish optimization.")

        # eleminate invalid individuals
        hof = [ind for ind in hof if ind.isValid()]

        # Hypervolume evolution (if possible)
        if self.__hv_logging and len(hof) > 0:
            hv = self.hypervolume([fit for sublist in fitness_hof_log for fit in sublist])
            if self.__hv_refpoint is None:
                self.__hv_refpoint = hv.refpoint(offset=0.1)   # Define global reference point
            hypervolume_log = [self.hypervolume(fit).compute(self.__hv_refpoint) \
                                if len(fit) > 0 else 0 for fit in fitness_hof_log]
            return hof, hypervolume_log
        else:
            return hof, None


