import sys, time, os, re, ipyparallel, torch, atexit, subprocess, signal, importlib
from ipyparallel import AsyncResult
from collections import OrderedDict
from typing import List
from torch.distributed import *

Debug = True
Verbose = True

def _debug(*args, **kwargs):
    if Debug: print(*args, file=sys.stderr, **kwargs)

def join_group_single(g_rank:int, l_rank:int, gpu:int, ws:int):
    '''Join the current process to a PyTorch distributed group.
    Todo -- parameterize all currently hardcoded values.
    '''
    import os, torch
    os.environ["RANK"] = str(g_rank) # Global rank
    os.environ["LOCAL_RANK"] = str(l_rank) # Local rank
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = str(29500)
    os.environ["WORLD_SIZE"] = str(ws)
    os.environ["OMP_NUM_THREADS"] = str(1) # See https://github.com/pytorch/pytorch/pull/22501
    torch.cuda.set_device(gpu)
    if ws > 1: torch.distributed.init_process_group(backend='nccl', init_method='env://')
    return os.environ["LOCAL_RANK"]

def exit_group_single():
    '''Exit the current process from the PyTorch distributed process group.'''
    for i in ["RANK", "LOCAL_RANK", "MASTER_ADDR", "MASTER_PORT", "WORLD_SIZE", "OMP_NUM_THREADS"]: os.environ.pop(i)
    torch.distributed.destroy_process_group()
    torch.cuda.empty_cache()

def meminfo():
    return [torch.cuda.memory_cached(), torch.cuda.memory_allocated()] if torch.cuda.is_available() else None

def freemem():
    '''Run gc.collect() and torch.cuda.empty_cache() if applicable.'''
    import gc
    gc.collect()
    if torch.cuda.is_available(): torch.cuda.empty_cache()
    return meminfo()

class IppCluster():
    """Start/stop of an ipyparallel cluster aka 'ipcluster, and access to cluster engines."""
    cid = "ippdpp_c"
    def __init__(self, n:int=0, engine_wait:float=15.0, **kwargs):
        popen_cmd = ["ipcluster", "start", f"--cluster-id={IppCluster.cid}", "--daemonize"]
        if n > 0: popen_cmd.append(f"--n={n}")

        cluster_proc = subprocess.Popen(popen_cmd) # Ignore stdout and stderr from ipcluster
        try: cluster_proc.wait(10)
        except subprocess.TimeoutExpired as e: raise TimeoutError("Timed out {popen_cmd}: {e}")

        try: self.client = ipyparallel.Client(timeout=engine_wait,cluster_id=f"{IppCluster.cid}")
        except (IOError,TimeoutError) as e: raise Exception(f"ipyparallel Client() failed: {e}")
        print(f"Connecting to ipyparallel cluster.", file=sys.stderr, end='', flush=True)
        while engine_wait > 0:
            try:
                self.px_view = self.client[:] # This will turn on ipyparallel's ipython line and cell magics
                engine_wait = 0
                pm = self.px_view.apply_async(os.getpid).get_dict()
                self.e_pids = [ pm[k] for k in sorted(pm) ]
                e_ppid = self.client[0].apply_sync(os.getppid)
                self.e_ppid = e_ppid

                def carefree_kill():
                    '''cleanup routine not tied to the object itself, ensure the object can be garbage collected after 'del' '''
                    subprocess.call(["ipcluster", "stop", f"--cluster-id={IppCluster.cid}"])
                    try:
                        os.kill(e_ppid, signal.SIGINT)
                        Verbose and print("pausing 3 seconds to let cluster shut down....", flush=True, file=sys.stderr)
                        time.sleep(3.0)
                    except ProcessLookupError: pass
                atexit.register(carefree_kill)
                self.carefree_kill = carefree_kill

            except ipyparallel.error.NoEnginesRegistered as e:
                print('.', file=sys.stderr, end='', flush=True)
                time.sleep(1)
                engine_wait -= 1

    def __del__(self): self.shutdown()

    def interrupt_engines(self, eids:List[int]):
        '''Send SIGINT to a list of ipyparallel engines, as if sending a keyboard interrupt to ipython running on the engine process.
         -- Note this does NOT kill the engine process.'''
        for i in eids: i < len(self.e_pids) and os.kill(self.e_pids[i], signal.SIGINT)

    def shutdown(self):
        '''Shutdown ipyparallel cluster -- this will kill all processes started by the 'ipcluster start' command.'''
        self.carefree_kill()
        self.px_view = self.client = self.e_pids = self.e_ppid = None
        
class Ddp():
    class StreamPrinter():
        ''' Each invocation prints from where it left off till the end of the current output '''
        _default_streaming_pause = 0.1
        def __init__(self, streams, *args, **kwargs):
            self._counts = [0] * len(streams)
            self._pause = kwargs.get('pause', self._default_streaming_pause)
        def __call__(self, streams, *args, **kwargs):
            for i, st in enumerate(streams):
                if (len(st) > self._counts[i]):
                    print(st[self._counts[i]:], end='', flush=True)
                    self._counts[i] = len(st)
            time.sleep(self._pause)

    def __init__(self, **kwargs):
        assert torch.cuda.is_available(), "CUDA not available! (Try reloading cuda driver?)"
        self.cluster = None
        self.ddp_group = None
        self._app = None

    def __del__(self): self.shutdown_cluster()

    def init_cluster(self, n_engines:int=0, **kwargs):
        self.cluster = IppCluster(n=n_engines, **kwargs)

    def shutdown_cluster(self):
        '''Properly shuts down the ipyparallel cluster (of engines).'''
        self.exit_group()
        if self.cluster:
            self.cluster.shutdown()
            self.cluster = None

    def app_init(self, appname:str):
        '''Configure additional application besides PyTorch op each ipyparallel engine process.
        The application module must have a `initializer()` function.'''
        app = importlib.import_module(f".{appname}", package=__package__)
        if app is None: raise NameError(f"Unknown app '{appname}' for Torch DDP. Have you installed {appname}.py?")
        
        self.app_exit() # Cleanup existing app

        if self._app is None:
            dv = self.cluster.px_view # shorthand for the DDP process group
            print(f"Importing on cluster: {app.imports}", flush=True)
            dv.execute(app.imports, block=True)
            r = dv.apply_sync(app.initializer)
            print(f"{appname}:\n", '\n'.join(r), sep='')
            self._app = app

    def app_exit(self):
        '''Execute the `finalizer()` call of the current application on all engines.'''
        if self._app:
            self.cluster.px_view.apply_sync(self._app.finalizer)
            self._app = None

    def gpus_str(self):
        return ','.join(map(str, self.ddp_group))

    def new_group(self, gpus:List[int], appname:str=None, node_rank:int=0, world_size:int=0):
        '''Configure a torch distributed process group of GPUs on a ipyparallel cluster.
        Returns a list of GPU ids of the group formed.'''
        (n_gpu, n_device) = (len(gpus), torch.cuda.device_count())
        if not self.cluster: self.init_cluster(n_engines=n_device)
        cl = self.cluster # shorthand
        assert n_gpu <= len(cl.client), f"More GPU ({gpus}) than ipyparallel engines ({len(cl.client)}). "
        assert max(gpus) < n_device, f"Invalid GPU id {max(gpus)}, highest allowed is {n_device-1}"

        Verbose and print(f"Initializing torch distributed group with GPUs {gpus}", flush=True)

        if world_size==0: world_size = n_gpu
        for rank,gpu in enumerate(gpus):
            dist_rank = n_gpu * node_rank + rank # see torch/distributed/launch.py
            cl.client[gpu].push(dict(g_rank=dist_rank, l_rank=rank, gpu=gpu, ws=world_size))

        # ipyparallel client[] accepts list of ints as slice indices.
        cl.px_view = cl.client[gpus]
        cl.px_view.execute(f'from {__name__} import join_group_single, exit_group_single, meminfo, freemem')
        cl.px_view.execute('r = join_group_single(g_rank=g_rank, l_rank=l_rank, gpu=gpu, ws=ws)', block=True)
        print("Local Ranks initialized: ", [ f"GPU{k}={v}" for k, v in cl.px_view.pull('r').get_dict().items()], flush=True)
        self.ddp_group = gpus

        if appname: self.app_init(appname)

    def exit_group(self):
        '''Tear down the PyTorch distributed process group on the ipyparallel cluster.'''
        self.app_exit()
        if self.ddp_group is None: return
        Verbose and print(f"DDP.exit_group(): {self.ddp_group}", flush=True)
        self.cluster.px_view.execute('exit_group_single()',block=True)
        self.cluster.px_view = self.ddp_group = None

    def meminfo(self):
        return self.cluster.px_view.apply_async(meminfo).get_dict() if self.cluster.px_view else None

    def gc(self):
        '''Calls gc.collect() and torch.cuda.empty_cache() if applicable.'''
        return self.cluster.px_view.apply_async(freemem).get_dict() if self.cluster.px_view else None

    def run_cell(self, cell, quiet:bool=False, gc:bool=True):
        baseline_mem = self.meminfo() if gc else None
        try:
            ar = self.cluster.px_view.execute(cell, silent=False, block=False) # silent=False to capture transient output
            watcher = self.StreamPrinter(ar.stdout) if (not quiet) else None

            if watcher:
                while not ar.ready(): # Simulate wait on blocking execution
                    watcher(ar.stdout)
                watcher(ar.stdout)
                ar.stdout = [] # already displayed, flush the streams.

        except KeyboardInterrupt:
            Verbose and print(f"Caugth interrupt, sending SIGINT to engines....", file=sys.stderr, flush=True)
            self.cluster.interrupt_engines(self.ddp_group)

        ar.display_outputs()

        if gc and (self.meminfo() != baseline_mem):
            m = self.gc() # ddp.gc() returns a dict keyed by engine id
            Verbose and print("GPU\tCached\tUsed", *[ f"{i}\t{m[i][0]}\t{m[i][1]}" for i in m ], sep='\n', flush=True)
        