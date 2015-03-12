from itertools import chain, islice
from collections import OrderedDict
import re
import json
import numpy as np
import theano
import theano.tensor as T

op_dict = {
  'exp': T.exp
}

# utils
def split(vec,sizes):
  return T.split(vec,sizes,len(sizes)) if len(sizes) > 1 else [vec]

def ensure_vector(x):
  return T.stack(x) if x.type.ndim == 0 else x

def inv(vec):
  if vec.ndim == 2:
    return np.linalg.inv(vec)
  elif vec.ndim == 0 or (vec.ndim == 1 and vec.size == 1):
      return 1.0/vec

def merge(*dlist):
  return dict(chain(*[d.items() for d in dlist]))

# HOMPACK90 STYLE
def row_dets(mat):
  (n,np1) = mat.shape
  qr = np.linalg.qr(mat,'r')
  dets = np.zeros(np1)
  dets[np1-1] = 1.0
  for lw in xrange(2,np1+1):
      i = np1 - lw
      ik = i + 1
      dets[i] = -np.dot(qr[i,ik:np1],dets[ik:np1])/qr[i,i]
  dets *= np.sign(np.prod(np.diag(qr))) # just for the sign
  #dets *= np.prod(np.diag(qr)) # to get the scale exactly
  return dets

class Model:
  def __init__(self,fname,constants={}):
    # parse model specification
    with open(fname,'r') as fid:
      mod = json.load(fid,object_pairs_hook=OrderedDict)

    # constants
    self.con_dict = OrderedDict()
    for name in mod['constants']:
      value = constants[name]
      self.con_dict[name] = np.array(value) if type(value) is list else value

    # arguments
    self.arg_info = OrderedDict()
    self.arg_dict = OrderedDict()
    for (name,spec) in mod['arguments'].items():
      asize = spec['size']
      (amin,amax) = spec['range']
      agrid = np.linspace(amin,amax,asize)

      info = OrderedDict()
      info['size'] = asize
      info['grid'] = agrid

      self.arg_info[name] = info
      self.arg_dict[name] = agrid

    # parameters
    self.par_info = OrderedDict()
    self.par_sizes = []
    for (name,spec) in mod['parameters'].items():
      ptype = spec.get('type','scalar')
      psize = 1 if ptype == 'scalar' else spec['size']

      info = OrderedDict()
      info['type'] = ptype
      info['size'] = psize

      self.par_info[name] = info
      self.par_sizes.append(psize)

    # variables
    self.var_info = OrderedDict()
    self.var_sizes = []
    for (name,spec) in mod['variables'].items():
      vtype = spec['type']

      info = OrderedDict()
      info['type'] = vtype

      if vtype == 'scalar':
        vsize = 1
        vder = 0
      elif vtype == 'vector':
        vsize = spec['size']
        vder = 0
      elif vtype == 'function':
        vder = spec.get('deriv',0)
        if vder > 0: info['nder'] = vder
        arg = spec['arg']
        info['arg'] = arg
        ainfo = self.arg_info[arg]
        vsize = ainfo['size']

      info['size'] = vsize
      self.var_info[name] = info
      self.var_sizes.append(vsize)
      if vder > 0: self.var_sizes += vder*[vsize]

    # totals
    self.n_pars = len(self.par_info)
    self.n_vars = len(self.var_info)

    self.sz_pars = np.sum(self.par_sizes)
    self.sz_vars = np.sum(self.var_sizes)

    # input vectors
    self.par_vec = T.dvector('parvec')
    self.var_vec = T.dvector('varvec')

    # unpack and map out variables
    self.par_dict = OrderedDict()
    piter = iter(split(self.par_vec,self.par_sizes))
    for (name,info) in self.par_info.items():
      ptype = info['type']
      par = piter.next()
      if ptype == 'scalar':
        self.par_dict[name] = par[0]
      else:
        self.par_dict[name] = par

    self.var_dict = OrderedDict()
    self.der_dict = OrderedDict()
    viter = iter(split(self.var_vec,self.var_sizes))
    for (name,info) in self.var_info.items():
      var = viter.next()
      vtype = info['type']
      if vtype == 'scalar':
        self.var_dict[name] = var[0]
      elif vtype == 'vector':
        self.var_dict[name] = var
      elif vtype == 'function':
        self.var_dict[name] = var
        if info.get('nder',0) > 0: self.der_dict[var] = list(islice(viter,info['nder']))

    # define derivative operator
    def diff(var,n=1):
      if n == 0:
        return var
      elif n > 0:
        return self.der_dict[var][n-1]
    self.diff_dict = {'diff':diff}

    # combine them all
    self.sym_dict = merge(op_dict,self.con_dict,self.par_dict,self.var_dict,self.diff_dict,self.arg_dict)

    # evaluate
    self.equations = []

    # regular equations
    for eq in mod['equations']:
      self.equations.append(eval(eq,{},self.sym_dict))

    # derivative relations
    for (name,info) in self.var_info.items():
      if info['type'] == 'function':
        var = self.var_dict[name]
        size = info['size']

        # derivative relations - symmetric except at 0
        nder = info.get('nder',0)
        if nder > 0:
          arg = info['arg']
          grid = self.arg_dict[arg]
          for d in xrange(nder):
            d0 = diff(var,d)
            d1 = diff(var,d+1)
            self.equations.append(d0[1]-d0[0]-(grid[1]-grid[0])*d1[0])
            self.equations.append((d0[2:]-d0[:-2])-(grid[2:]-grid[:-2])*d1[1:-1])

    # repack
    self.eqn_vec = T.join(0,*map(ensure_vector,self.equations))

    # jacobians
    self.par_jac = T.jacobian(self.eqn_vec,self.par_vec)
    self.var_jac = T.jacobian(self.eqn_vec,self.var_vec)

    # compile
    print 'Compiling...'
    self.eqn_fun = theano.function([self.par_vec,self.var_vec],self.eqn_vec)
    self.parjac_fun = theano.function([self.par_vec,self.var_vec],self.par_jac)
    self.varjac_fun = theano.function([self.par_vec,self.var_vec],self.var_jac)

    # newtonian path
    t = T.dscalar('t')
    start = T.dvector('start')
    finish = T.dvector('finish')
    path = (1.0-t)*start + t*finish
    dpath = T.jacobian(path,t)
    self.path_fun = theano.function([start,finish,t],path)
    self.dpath_fun = theano.function([start,finish,t],dpath)

  def dict_to_array(self,din,dinfo):
    vout = []
    for (var,info) in dinfo.items():
      val = din[var]
      vtype = info['type']
      if vtype == 'scalar':
        vout += [val]
      elif vtype == 'vector':
        vout += list(val)
      elif vtype == 'function':
        if info.get('nder',0) == 0:
          vout += list(val)
        else:
          vout += list(np.concatenate(val))
    return np.array(vout)

  def array_to_dict(self,vin,dinfo,sizes):
    dout = OrderedDict()
    viter = iter(np.split(vin,np.cumsum(sizes)))
    for (var,info) in dinfo.items():
      vtype = info['type']
      size = info['size']
      if vtype == 'scalar':
        dout[var] = viter.next()[0]
      elif vtype == 'vector':
        dout[var] = viter.next()
      if vtype == 'function':
        nder = info.get('nder',0)
        if nder == 0:
          dout[var] = viter.next()
        else:
          dout[var] = list(islice(viter,nder+1))
    return dout

  def eval_system(self,par_dict,var_dict,output=False):
    par_val = self.dict_to_array(par_dict,self.par_info)
    var_val = self.dict_to_array(var_dict,self.var_info)
    eqn_val = self.eqn_fun(par_val,var_val)

    if output:
      print 'par_val = {}'.format(str(par_val))
      print 'var_val = {}'.format(str(var_val))
      print 'eqn_val = {}'.format(str(eqn_val))
      print

    return eqn_val

  # solve system, possibly along projection (otherwise fix t)
  def solve_system(self,par_dict,var_dict,eqn_tol=1.0e-12,max_rep=100,output=False,plot=False):
    par_val = self.dict_to_array(par_dict,self.par_info)
    var_val = self.dict_to_array(var_dict,self.var_info)
    eqn_val = self.eqn_fun(par_val,var_val)

    for i in xrange(max_rep):
      varjac_val = self.varjac_fun(par_val,var_val)
      step = -np.linalg.solve(varjac_val,eqn_val)
      var_val += step
      eqn_val = self.eqn_fun(par_val,var_val)

      if np.max(np.abs(eqn_val)) <= eqn_tol: break

    if output:
      print 'Equation Solved ({})'.format(i)
      #print 'par_val = {}'.format(str(par_val))
      #print 'var_val = {}'.format(str(var_val))
      #print 'eqn_val = {}'.format(str(eqn_val))
      print

    return self.array_to_dict(var_val,self.var_info,self.var_sizes)

  def homotopy_bde(self,par_start_dict,par_finish_dict,var_start_dict,delt=0.01,eqn_tol=1.0e-12,max_step=1000,max_newton=10,solve=False,output=False,out_rep=5,plot=False):
    # refine initial solution if needed
    if solve: var_start_dict = self.solve_system(par_start_dict,var_start_dict,output=output)

    # convert to raw arrays
    par_start = self.dict_to_array(par_start_dict,self.par_info)
    par_finish = self.dict_to_array(par_finish_dict,self.par_info)
    var_start = self.dict_to_array(var_start_dict,self.var_info)

    # generate analytic homotopy paths
    path_apply = lambda t: self.path_fun(par_start,par_finish,t)
    dpath_apply = lambda t: self.dpath_fun(par_start,par_finish,t)

    # start path
    tv = 0.0

    if output:
      print 't = {}'.format(tv)
      #print 'par_val = {}'.format(par_start)
      #print 'var_val = {}'.format(var_start)
      print 'Equation error = {}'.format(np.max(np.abs(self.eqn_fun(par_start,var_start))))
      print

    # save path
    t_path = [tv]
    par_path = [par_start]
    var_path = [var_start]

    direc = None
    var_val = var_start.copy()
    for rep in xrange(max_step):
      # calculate jacobians
      par_val = path_apply(tv)
      dpath_val = dpath_apply(tv)
      varjac_val = self.varjac_fun(par_val,var_val)
      parjac_val = self.parjac_fun(par_val,var_val)

      # calculate steps
      tdir_val = np.dot(parjac_val,dpath_val)[:,np.newaxis]
      fulljac_val = np.hstack([varjac_val,tdir_val])
      step_pred = row_dets(fulljac_val)

      if np.mean(np.abs(step_pred)) == 0.0:
        # elevator step
        step_pred[:] = np.zeros_like(step_pred)
        step_pred[-1] = np.minimum(delt,1.0-tv)
        direc = None
      else:
        # move in the right direction
        if direc is None: direc = np.sign(step_pred[-1])
        step_pred *= direc

        # this normalization keeps us in sane regions
        #step_pred *= delt
        step_pred *= delt/np.mean(np.abs(step_pred))

        # bound between [0,1] and limit step size
        delt_max = np.minimum(delt,1.0-tv)
        delt_min = np.maximum(-delt,-tv)
        if step_pred[-1] > delt_max: step_pred *= delt_max/step_pred[-1]
        if step_pred[-1] < delt_min: step_pred *= delt_min/step_pred[-1]

      # increment
      tv += step_pred[-1]
      var_val += step_pred[:-1]

      # new function value
      par_val = path_apply(tv)
      eqn_val = self.eqn_fun(par_val,var_val)

      # store
      t_path.append(tv)
      par_path.append(par_val.copy())
      var_path.append(var_val.copy())

      # projection steps
      tv0 = tv
      var_val0 = var_val.copy()
      for i in xrange(max_newton):
        if tv == 0.0 or tv == 1.0:
          proj_dir = np.r_[np.zeros(self.sz_vars),1.0]
        else:
          proj_dir = step_pred # project along previous step

        dpath_val = dpath_apply(tv)
        varjac_val = self.varjac_fun(par_val,var_val)
        parjac_val = self.parjac_fun(par_val,var_val)
        tdir_val = np.dot(parjac_val,dpath_val)[:,np.newaxis]

        fulljac_val = np.hstack([varjac_val,tdir_val])
        projjac_val = np.vstack([fulljac_val,proj_dir])
        step_corr = -np.linalg.solve(projjac_val,np.r_[eqn_val,0.0])

        tv += step_corr[-1]
        par_val = path_apply(tv)
        var_val += step_corr[:-1]
        eqn_val = self.eqn_fun(par_val,var_val)

        if np.max(np.abs(eqn_val)) <= eqn_tol: break

      if output and rep%out_rep==0:
        print 'Iteration = {}'.format(rep)
        print 'Step predict = {}'.format(step_pred[-1])
        print 'Correction steps = {}'.format(i)
        print 't = {}'.format(tv)
        #print 'par_val = {}'.format(str(par_val))
        #print 'var_val = {}'.format(str(var_val))
        print 'Equation error = {}'.format(np.max(np.abs(eqn_val)))
        print

      # store
      t_path.append(tv)
      par_path.append(par_val.copy())
      var_path.append(var_val.copy())

      # if we can't stay on the path
      if (np.max(np.abs(eqn_val)) > eqn_tol) or np.isnan(eqn_val).any():
        print 'Off the rails.'
        break

      # break at end
      if tv <= 0.0 or tv >= 1.0: break

    (t_path,par_path,var_path) = map(np.array,(t_path,par_path,var_path))

    if output:
      print 'Done at {}!'.format(rep)
      print 't = {}'.format(tv)
      print 'Equation error = {}'.format(np.max(np.abs(eqn_val)))

    if plot:
      import matplotlib.pylab as plt
      plt.scatter(var_path[1::2],t_path[1::2],c='r')
      plt.scatter(var_path[::2],t_path[::2],c='b')

    return (t_path,par_path,var_path)

def diff_eq_config(eq,bound,trange,xvar='x',N=32):
  (tmin,tmax) = trange
  tgrid = np.linspace(tmin,tmax,N)
  dgrid = tgrid[1:] - tgrid[:-1]

  dvar = 'dx'
  def d(v):
    if v == xvar:
      return dvar

  eq0 = eq.replace('d({})'.format(xvar),'d'+xvar)

  rvar = r'((?<=[^a-zA-Z]){0:}(?=(\Z|[^a-zA-Z]))|(?<=\A){0:}(?=(\Z|[^a-zA-Z])))'
  def evali(i):
    istr = str(i)
    eqp = eq0
    (eqp,_) = re.subn(rvar.format(xvar),xvar+istr,eqp)
    (eqp,_) = re.subn(rvar.format(dvar),dvar+istr,eqp)
    return eqp
  eqs = [evali(i) for i in xrange(N)]

  def dsumi(i):
    istr = str(i)
    i1str = str(i+1)
    eqd = xvar+i1str+' - '+xvar+istr+' - '+'dt'+istr+'*'+'dx'+istr
    return eqd
  eqds = [dsumi(i) for i in xrange(N-1)]

  (bloc,bval) = bound
  bmin = (tgrid>bloc).nonzero()[0][0] - 1
  bfrac = bloc - tgrid[bmin]
  if bfrac == 0.0:
    eqb = xvar+str(bmin)+' - '+str(bval)
  else:
    eqb = str(1.0-bfrac)+'*'+xvar+str(bmin)+'+'+str(bfrac)+'*'+xvar+str(bmin+1)+' - '+str(bval)

  return (eqs,eqds,eqb)

# Usage
# mod = Model('model.json')
# (t_path,par_path,var_path) = mod.homotopy_bde({"z": 2.0},{"z": 1.0},{"x": 1.0})
# (t_path,par_path,var_path) = mod.homotopy_bde({"z": 2.0},{"z": 3.0},{"x": 1.0})
