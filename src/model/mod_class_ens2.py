"""Dalecv2 model class takes a data class and then uses functions to run the
dalecv2 model.
"""
import numpy as np
import scipy.optimize as spop
import pickle
import algopy
import emcee
import joblib as jl
import random as rand
import multiprocessing
import copy as cp
import my_email


class DalecModel():

    def __init__(self, dataclass, time_step=0, strtrun=0, size_ens='None'):
        """dataClass and timestep at which to run the dalecv2 model.
        """
        self.dC = dataclass
        self.x = time_step
        if self.dC.k is None:
            self.lenrun = self.dC.len_run
        else:
            self.lenrun = self.dC.len_run*self.dC.k
        self.xb = self.dC.xb
        self.opt_xb = self.dC.opt_xb
        self.opt_B = self.dC.opt_B
        self.modcoston = True
        self.modobdict = {'gpp': self.gpp, 'nee': self.nee, 'nee_day': self.nee_day,
                          'nee_night': self.nee_night, 'rt': self.rt,
                          'cf': self.cf, 'clab': self.clab, 'c_roo': self.cr,
                          'c_woo': self.cw, 'cl': self.cl, 'cs': self.cs,
                          'lf': self.lf, 'lw': self.lw, 'lai': self.lai, 'clma': self.clma,
                          'litresp': self.litresp, 'soilresp': self.soilresp,
                          'rtot': self.rtot, 'rh': self.rh, 'ra': self.ra,
                          'd_onset': self.d_onset, 'groundresp': self.groundresp,
                          'phi_onset': self.phi_on, 'phi_fall': self.phi_off}
        self.startrun = strtrun
        self.endrun = self.lenrun
        self.yoblist, self.yerroblist, self.ytimestep, self.y_strlst = self.obscost()
        self.rmatrix = self.rmat(self.yerroblist)
        self.obs_time_step = self.no_obs_at_time()
        self.diag_b = np.diag(np.diag(self.dC.B))
        self.b_tilda = np.dot(np.dot(np.linalg.inv(np.sqrt(self.diag_b)), self.dC.B),
                              np.linalg.inv(np.sqrt(self.diag_b)))
        if size_ens is not 'None':
            self.size_ens = size_ens
            self.xbs, self.xb_mat = self.create_ensemble2(size_ens)
            self.xb_mat_inv = np.linalg.pinv(self.xb_mat.T)
        self.nume = 100


# ------------------------------------------------------------------------------
# Model functions
# ------------------------------------------------------------------------------

    @staticmethod
    def fit_polynomial(ep, mult_fac):
        """ Polynomial used to find phi_f and phi (offset terms used in
        phi_onset and phi_fall), given an evaluation point for the polynomial
        and a multiplication term.
        :param ep: evaluation point
        :param mult_fac: multiplication term
        :return: fitted polynomial value
        """
        cf = [2.359978471e-05, 0.000332730053021, 0.000901865258885,
              -0.005437736864888, -0.020836027517787, 0.126972018064287,
              -0.188459767342504]
        poly_val = cf[0]*ep**6 + cf[1]*ep**5 + cf[2]*ep**4 + cf[3]*ep**3 + cf[4]*ep**2 + \
            cf[5]*ep**1 + cf[6]*ep**0
        phi = poly_val*mult_fac
        return phi

    def temp_term(self, Theta, temperature):
        """ Calculates the temperature exponent factor for carbon pool
        respiration's given a value for Theta parameter.
        :param Theta: temperature dependence exponent factor
        :return: temperature exponent respiration
        """
        temp_term = np.exp(Theta*temperature)
        return temp_term

    def acm(self, cf, clma, ceff, acm):
        """ Aggregated canopy model (ACM) function
        ------------------------------------------
        Takes a foliar carbon (cf) value, leaf mass per area (clma) and canopy
        efficiency (ceff) and returns the estimated value for Gross Primary
        Productivity (gpp) of the forest at that time.
        :param cf: foliar carbon (g C m-2)
        :param clma: leaf mass area (g C m-2_
        :param ceff: canopy efficiency parameter
        :return: GPP value
        """
        t_range = 0.5*(self.dC.t_max[self.x] - self.dC.t_min[self.x])
        L = cf / clma
        q = acm[1] - acm[2]
        gc = (abs(self.dC.phi_d))**acm[8] / \
             (t_range + acm[4]*self.dC.R_tot)
        p = ((ceff*L) / gc)*np.exp(acm[6]*self.dC.t_max[self.x])
        ci = 0.5*(self.dC.ca + q - p + np.sqrt((self.dC.ca + q - p)**2
                  - 4*(self.dC.ca*q - p*acm[1])))
        E0 = (acm[5]*L**2) / (L**2 + acm[7])
        delta = -23.4*np.cos((360.*(self.dC.D[self.x] + 10) / 365.) *
                             (np.pi/180.))*(np.pi/180.)
        s = 24*np.arccos((- np.tan(self.dC.lat)*np.tan(delta))) / np.pi
        if s >= 24.:
            s = 24.
        elif s <= 0.:
            s = 0.
        else:
            s = s
        gpp = (E0*self.dC.I[self.x]*gc*(self.dC.ca - ci))*(acm[0]*s +
                                                           acm[3]) / \
              (E0*self.dC.I[self.x] + gc*(self.dC.ca - ci))
        return gpp

    def phi_onset(self, d_onset, cronset):
        """Leaf onset function (controls labile to foliar carbon transfer)
        takes d_onset value, cronset value and returns a value for phi_onset.
        """
        release_coeff = np.sqrt(2.)*cronset / 2.
        mag_coeff = (np.log(1.+1e-3) - np.log(1e-3)) / 2.
        offset = self.fit_polynomial(1+1e-3, release_coeff)
        phi_onset = (2. / np.sqrt(np.pi))*(mag_coeff / release_coeff) * \
            np.exp(-(np.sin((self.dC.D[self.x] - d_onset + offset) /
                     self.dC.radconv)*(self.dC.radconv / release_coeff))**2)
        return phi_onset

    def phi_fall(self, d_fall, crfall, clspan):
        """Leaf fall function (controls foliar to litter carbon transfer) takes
        d_fall value, crfall value, clspan value and returns a value for
        phi_fall.
        """
        release_coeff = np.sqrt(2.)*crfall / 2.
        mag_coeff = (np.log(clspan) - np.log(clspan - 1.)) / 2.
        offset = self.fit_polynomial(clspan, release_coeff)
        phi_fall = (2. / np.sqrt(np.pi))*(mag_coeff / release_coeff) * \
            np.exp(-(np.sin((self.dC.D[self.x] - d_fall + offset) /
                   self.dC.radconv)*self.dC.radconv / release_coeff)**2)
        return phi_fall

    def dalecv2(self, p):
        """DALECV2 carbon balance model
        -------------------------------
        evolves carbon pools to the next time step, taking the 6 carbon pool
        values and 17 parameters at time t and evolving them to time t+1.
        Outputs both the 6 evolved C pool values and the 17 constant parameter
        values.

        phi_on = phi_onset(d_onset, cronset)
        phi_off = phi_fall(d_fall, crfall, clspan)
        gpp = acm(cf, clma, ceff)
        temp = temp_term(Theta)

        clab2 = (1 - phi_on)*clab + (1-f_auto)*(1-f_fol)*f_lab*gpp
        cf2 = (1 - phi_off)*cf + phi_on*clab + (1-f_auto)*f_fol*gpp
        cr2 = (1 - theta_roo)*cr + (1-f_auto)*(1-f_fol)*(1-f_lab)*f_roo*gpp
        cw2 = (1 - theta_woo)*cw + (1-f_auto)*(1-f_fol)*(1-f_lab)*(1-f_roo)*gpp
        cl2 = (1-(theta_lit+theta_min)*temp)*cl + theta_roo*cr + phi_off*cf
        cs2 = (1 - theta_som*temp)*cs + theta_woo*cw + theta_min*temp*cl
        """
        out = algopy.zeros(23, dtype=p)

        phi_on = self.phi_onset(p[11], p[13])
        phi_off = self.phi_fall(p[14], p[15], p[4])
        gpp = self.acm(p[18], p[16], p[10], self.dC.acm)
        temp = self.temp_term(p[9], self.dC.t_mean[self.x])

        out[17] = (1 - phi_on)*p[17] + (1-p[1])*(1-p[2])*p[12]*gpp
        out[18] = (1 - phi_off)*p[18] + phi_on*p[17] + (1-p[1])*p[2]*gpp
        out[19] = (1 - p[6])*p[19] + (1-p[1])*(1-p[2])*(1-p[12])*p[3]*gpp
        out[20] = (1 - p[5])*p[20] + (1-p[1])*(1-p[2])*(1-p[12])*(1-p[3])*gpp
        out[21] = (1-(p[7]+p[0])*temp)*p[21] + p[6]*p[19] + phi_off*p[18]
        out[22] = (1 - p[8]*temp)*p[22] + p[5]*p[20] + p[0]*temp*p[21]
        out[0:17] = p[0:17]
        return out


    def dalecv2diff(self, p):
        """DALECV2 carbon balance model
        -------------------------------
        evolves carbon pools to the next time step, taking the 6 carbon pool
        values and 17 parameters at time t and evolving them to time t+1.
        Ouputs an array of just the 6 evolved C pool values.

        phi_on = phi_onset(d_onset, cronset)
        phi_off = phi_fall(d_fall, crfall, clspan)
        gpp = acm(cf, clma, ceff)
        temp = temp_term(Theta)

        clab2 = (1 - phi_on)*clab + (1-f_auto)*(1-f_fol)*f_lab*gpp
        cf2 = (1 - phi_off)*cf + phi_on*clab + (1-f_auto)*f_fol*gpp
        cr2 = (1 - theta_roo)*cr + (1-f_auto)*(1-f_fol)*(1-f_lab)*f_roo*gpp
        cw2 = (1 - theta_woo)*cw + (1-f_auto)*(1-f_fol)*(1-f_lab)*(1-f_roo)*gpp
        cl2 = (1-(theta_lit+theta_min)*temp)*cl + theta_roo*cr + phi_off*cf
        cs2 = (1 - theta_som*temp)*cs + theta_woo*cw + theta_min*temp*cl
        """
        out = algopy.zeros(6, dtype=p)

        phi_on = self.phi_onset(p[11], p[13])
        phi_off = self.phi_fall(p[14], p[15], p[4])
        gpp = self.acm(p[18], p[16], p[10], self.dC.acm)
        temp = self.temp_term(p[9], self.dC.t_mean[self.x])

        out[0] = (1 - phi_on)*p[17] + (1-p[1])*(1-p[2])*p[12]*gpp
        out[1] = (1 - phi_off)*p[18] + phi_on*p[17] + (1-p[1])*p[2]*gpp
        out[2] = (1 - p[6])*p[19] + (1-p[1])*(1-p[2])*(1-p[12])*p[3]*gpp
        out[3] = (1 - p[5])*p[20] + (1-p[1])*(1-p[2])*(1-p[12])*(1-p[3])*gpp
        out[4] = (1-(p[7]+p[0])*temp)*p[21] + p[6]*p[19] + phi_off*p[18]
        out[5] = (1 - p[8]*temp)*p[22] + p[5]*p[20] + p[0]*temp*p[21]
        return out

    def jac_dalecv2(self, p):
        """Using algopy package calculates the jacobian for dalecv2 given a
        input vector p.
        """
        p = algopy.UTPM.init_jacobian(p)
        return algopy.UTPM.extract_jacobian(self.dalecv2(p))

    def jac2_dalecv2(self, p):
        """Use algopy reverse mode ad calc jac of dv2.
        """
        mat = np.ones((23, 23))*-9999.
        mat[0:17] = np.eye(17, 23)
        p = algopy.UTPM.init_jacobian(p)
        mat[17:] = algopy.UTPM.extract_jacobian(self.dalecv2diff(p))
        return mat

    def mod_list(self, pvals):
        """Creates an array of evolving model values using dalecv2 function.
        Takes a list of initial param values.
        """
        mod_list = np.concatenate((np.array([pvals]),
                                  np.ones((self.endrun-self.startrun, len(pvals)))*-9999.))

        self.x = self.startrun
        for t in xrange(self.endrun-self.startrun):
            mod_list[(t+1)] = self.dalecv2(mod_list[t])
            self.x += 1

        self.x -= self.endrun
        return mod_list

    def linmod_list(self, pvals):
        """Creates an array of linearized models (Mi's) taking a list of
        initial param values and a run length (lenrun).
        """
        mod_list = np.concatenate((np.array([pvals]),
                                  np.ones((self.endrun-self.startrun, len(pvals)))*-9999.))
        matlist = np.ones((self.endrun-self.startrun, 23, 23))*-9999.

        self.x = self.startrun
        for t in xrange(self.endrun-self.startrun):
            mod_list[(t+1)] = self.dalecv2(mod_list[t])
            matlist[t] = self.jac2_dalecv2(mod_list[t])
            self.x += 1

        self.x -= self.endrun
        return mod_list, matlist

    @staticmethod
    def mfac(matlist, timestep):
        """matrix factorial function, takes a list of matrices and a time step,
        returns the matrix factoral.
        """
        if timestep == -1.:
            return np.eye(23)
        mat = matlist[0]
        for t in xrange(0, timestep):
            mat = np.dot(matlist[t+1], mat)
        return mat

    def evolve_mat(self, mat, matlist):
        evolve_mat = mat
        for m in matlist:
            evolve_mat = np.dot(np.dot(m, evolve_mat), m.T)
        return evolve_mat


# ------------------------------------------------------------------------------
# Observation functions
# ------------------------------------------------------------------------------

    def gpp(self, p):
        """Function calculates gross primary production (gpp).
        """
        gpp = self.acm(p[18], p[16], p[10], self.dC.acm)
        return gpp

    def rt(self, p):
        """Function calculates total ecosystem respiration (rec).
        """
        rec = p[1]*self.acm(p[18], p[16], p[10], self.dC.acm) + \
            (p[7]*p[21] + p[8]*p[22])*self.temp_term(p[9], self.dC.t_mean[self.x])
        return rec

    def nee(self, p):
        """Function calculates Net Ecosystem Exchange (nee).
        """
        nee = -(1. - p[1])*self.acm(p[18], p[16], p[10], self.dC.acm) + \
            (p[7]*p[21] + p[8]*p[22])*self.temp_term(p[9], self.dC.t_mean[self.x])
        return nee

    def nee_day(self, p):
        """Function calculates daytime Net Ecosystem Exchange (nee).
        """
        nee = -(1. - (self.dC.day_len[self.x]/24.)*p[1])*self.acm(p[18], p[16], p[10], self.dC.acm) + \
               (self.dC.day_len[self.x]/24.)*(p[7]*p[21] + p[8]*p[22])*self.temp_term(p[9], self.dC.t_day[self.x])
        return nee

    def nee_night(self, p):
        """Function calculates nighttime Net Ecosystem Exchange (nee).
        """
        nee = (self.dC.night_len[self.x]/24.)*p[1]*self.acm(p[18], p[16], p[10], self.dC.acm) + \
              (self.dC.night_len[self.x]/24.)*(p[7]*p[21] + p[8]*p[22])*self.temp_term(p[9], self.dC.t_night[self.x])
        return nee

    def litresp(self, p):
        """Function calculates litter respiration (litresp).
        """
        litresp = p[7]*p[21]*self.temp_term(p[9], self.dC.t_mean[self.x])
        return litresp

    def soilresp(self, p):
        """Function calculates soil respiration (soilresp). (heterotrophic)
        """
        soilresp = p[8]*p[22]*self.temp_term(p[9], self.dC.t_mean[self.x])
        return soilresp

    def groundresp(self, p):
        """Function calculates ground respiration from soil chamber measurements
        """
        groundresp = p[7]*p[21]*self.temp_term(p[9], self.dC.t_mean[self.x]) + \
                    p[8]*p[22]*self.temp_term(p[9], self.dC.t_mean[self.x]) + \
                    (1./3.)*p[1]*self.acm(p[18], p[16], p[10], self.dC.t_mean[self.x])
        return groundresp

    def rh(self, p):
        """Fn calculates rh (soilresp+litrep).
        """
        rh = (p[7]*p[21] + p[8]*p[22])*self.temp_term(p[9], self.dC.t_mean[self.x])
        return rh

    def ra(self, p):
        """Fn calculates ra (autotrophic resp.).
        """
        ra = p[1]*self.acm(p[18], p[16], p[10], self.dC.acm)
        return ra

    def rtot(self, p):
        """Function calculates soil + root respiration (soilrootresp).
        """
        rtot = p[8]*p[22]*self.temp_term(p[9], self.dC.t_mean[self.x]) + 5. #Figure this out boi!
        return rtot

    def lai(self, p):
        """Fn calculates leaf area index (cf/clma).
        """
        lai = p[18] / p[16]
        return lai

    def clma(self, p):
        """Fn calculates clma (carbon leaf mass per area)
        """
        clma = p[16]
        return clma

    def lf(self, p):
        """Fn calulates litter fall.
        """
        lf = self.phi_fall(p[14], p[15], p[4])*p[18]
        return lf

    def lw(self, p):
        """Fn calulates litter fall.
        """
        lw = p[5]*p[20]
        return lw

    def clab(self, p):
        """Fn calulates labile carbon.
        """
        clab = p[17]
        return clab

    def cf(self, p):
        """Fn calulates foliar carbon.
        """
        cf = p[18]
        return cf

    def cr(self, p):
        """Fn calulates root carbon.
        """
        cr = p[19]
        return cr

    def cw(self, p):
        """Fn calulates woody biomass carbon.
        """
        cw = p[20]
        return cw

    def cl(self, p):
        """Fn calulates litter carbon.
        """
        cl = p[21]
        return cl

    def cs(self, p):
        """Fn calulates soil organic matter carbon.
        """
        cs = p[22]
        return cs

    def d_onset(self, p):
        """Fn calculates day of leaf on,
        """
        d_onset = p[11]
        return d_onset

    def phi_on(self, p):
        """Fn calculates day of leaf on,
        """
        phi_on_ob = self.phi_onset(p[11], p[13])
        return phi_on_ob

    def phi_off(self, p):
        """Fn calculates day of leaf on,
        """
        phi_off_ob = self.phi_fall(p[14], p[15], p[4])
        return phi_off_ob

    def linob(self, ob, pvals):
        """Function returning jacobian of observation with respect to the
        parameter list. Takes an obs string, a parameters list, a dataClass
        and a time step x.
        """
        dpvals = algopy.UTPM.init_jacobian(pvals)
        return algopy.UTPM.extract_jacobian(self.modobdict[ob](dpvals))

    def oblist(self, ob, mod_list):
        oblist = np.ones(self.endrun-self.startrun)*-9999.
        self.x = self.startrun
        for t in xrange(self.endrun-self.startrun):
            oblist[t] = self.modobdict[ob](mod_list[t])
            self.x += 1
        self.x -= self.endrun
        return oblist

# ------------------------------------------------------------------------------
# Assimilation functions
# ------------------------------------------------------------------------------

    def obscost(self):
        """Function returning list of observations and a list of their
        corresponding error values. Takes observation dictionary and an
        observation error dictionary.
        """
        yoblist = np.array([])
        yerrlist = np.array([])
        ytimestep = np.array([])
        y_strlst = np.array([])
        for t in xrange(self.startrun, self.endrun):
            for ob in self.dC.ob_dict.iterkeys():
                if np.isnan(self.dC.ob_dict[ob][t]) != True:
                    yoblist = np.append(yoblist, self.dC.ob_dict[ob][t])
                    yerrlist = np.append(yerrlist,
                                         self.dC.ob_err_dict[ob][t])
                    ytimestep = np.append(ytimestep, t)
                    y_strlst = np.append(y_strlst, ob)
        return yoblist, yerrlist, ytimestep, y_strlst

    def hxcost(self, pvallist):
        """Function returning a list of observation values as predicted by the
        DALEC model. Takes a list of model values (pvallist), an observation
        dictionary and a dataClass (dC).
        """
        hx = np.array([])
        self.x = self.startrun
        for t in xrange(self.startrun, self.endrun):
            for ob in self.dC.ob_dict.iterkeys():
                if np.isnan(self.dC.ob_dict[ob][t]) != True:
                    hx = np.append(hx,
                                   self.modobdict[ob](pvallist[t-self.startrun]))
            self.x += 1

        self.x -= self.endrun
        return hx

    def rmat(self, yerr):
        """Returns observation error covariance matrix given a list of
        observation error values.
        """
        r = (yerr**2)*np.eye(len(yerr))
        return r

    def no_obs_at_time(self):
        """ Returns a list of the number of observations at each time step.
        """
        obs_time_step = np.array([])
        self.x = self.startrun
        for t in xrange(self.startrun, self.endrun):
            p = 0
            for ob in self.dC.ob_dict.iterkeys():
                if np.isnan(self.dC.ob_dict[ob][t]) != True:
                    p += 1
            obs_time_step = np.append(obs_time_step, p)
            self.x += 1

        self.x -= self.endrun
        return obs_time_step

    def gradcost2(self, pvals):
        """Gradient of 4DVAR cost fn to be passed to optimization routine.
        Takes an initial state (pvals), an obs dictionary, an obs error
        dictionary, a dataClass and a start and finish time step. Using Lagrange
        multipliers to increase speed, method updated to allow for temporally
        correlated R matrix. Uses method of Lagrange multipliers!
        """
        pvallist, matlist = self.linmod_list(pvals)
        hx, hhat = self.hhat(pvallist)
        r_yhx = np.dot(np.linalg.inv(self.rmatrix), (self.yoblist-hx).T)
        idx1 = len(self.yoblist) - sum(self.obs_time_step[self.lenrun-1:])
        idx2 = len(self.yoblist) - sum(self.obs_time_step[self.lenrun-1+1:])
        obcost = np.dot(hhat[idx1:idx2].T, r_yhx[idx1:idx2])
        for i in xrange(self.lenrun-2, -1, -1):
            if self.obs_time_step[i] != 0:
                idx1 = len(self.yoblist) - sum(self.obs_time_step[i:])
                idx2 = len(self.yoblist) - sum(self.obs_time_step[i+1:])
                obcost = np.dot(matlist[i].T, obcost) + np.dot(hhat[idx1:idx2].T, r_yhx[idx1:idx2])
            else:
                obcost = np.dot(matlist[i].T, obcost)

        if self.modcoston is True:
            modcost = np.dot(np.linalg.inv(self.dC.B), (pvals-self.xb).T)
        else:
            modcost = 0
        gradcost = - obcost + modcost
        return gradcost

    def hhat(self, pvallist):
        """Returns a list of observation values as predicted by DALEC (hx) and
        a stacked set of linearzied observation operators (hmat) for use in gradcost2
        fn calculating the gradient of the cost fn using the method of Lagrange multipliers.
        Takes a list of model values (pvallist), a observation dictionary, a list of
        linearized models (matlist) and a dataClass (dC).
        """
        hx = np.array([])
        hhat = []
        self.x = self.startrun
        for t in xrange(self.startrun, self.endrun):
            temp = []
            for ob in self.dC.ob_dict.iterkeys():
                if np.isnan(self.dC.ob_dict[ob][t]) != True:
                    hx = np.append(hx,
                                   self.modobdict[ob](pvallist[t-self.startrun]))
                    temp.append([self.linob(ob, pvallist[t-self.startrun])])
            self.x += 1
            if len(temp) != 0.:
                hhat.append(np.vstack(temp))
            else:
                continue

        self.x -= self.endrun
        return hx, np.vstack(hhat)

    def hmat(self, pvallist, matlist):
        """Returns a list of observation values as predicted by DALEC (hx) and
        a linearzied observation error covariance matrix (hmat). Takes a list
        of model values (pvallist), a observation dictionary, a list of
        linearized models (matlist) and a dataClass (dC).
        """
        hx = np.array([])
        hmat = np.array([])
        self.x = self.startrun
        for t in xrange(self.startrun, self.endrun):
            temp = []
            for ob in self.dC.ob_dict.iterkeys():
                if np.isnan(self.dC.ob_dict[ob][t]) != True:
                    hx = np.append(hx,
                                   self.modobdict[ob](pvallist[t-self.startrun]))
                    temp.append([self.linob(ob, pvallist[t-self.startrun])])
            self.x += 1
            if len(temp) != 0.:
                hmat = np.append(hmat, np.dot(np.vstack(temp),
                                 self.mfac(matlist, t-self.startrun-1)))
            else:
                continue

        self.x -= self.endrun
        hmat = np.reshape(hmat, (len(hmat)/23, 23))
        return hx, hmat

    def modcost(self, pvals):
        """model part of cost fn.
        """
        return np.dot(np.dot((pvals-self.opt_xb), np.linalg.inv(self.dC.opt_B)), (pvals-self.opt_xb).T)

    def obcost(self, pvals):
        """Observational part of cost fn.
        """
        pvallist = self.mod_list(pvals)
        hx = self.hxcost(pvallist)
        return np.dot(np.dot((self.yoblist-hx), np.linalg.inv(self.rmatrix)), (self.yoblist-hx).T)

    def cost(self, pvals_opt):
        """4DVAR cost function to be minimized. Takes an initial state (pvals),
        an observation dictionary, observation error dictionary, a dataClass
        and a start and finish time step.
        """
        pvals = cp.copy(self.xb)
        pvals[self.dC.params4opt] = pvals_opt
        ob_cost = self.obcost(pvals)
        if self.modcoston is True:
            mod_cost = self.modcost(pvals_opt)
        else:
            mod_cost = 0
        cost = 0.5*ob_cost + 0.5*mod_cost
        return cost

    def gradcost(self, pvals_opt):
        """Gradient of 4DVAR cost fn to be passed to optimization routine.
        Takes an initial state (pvals), an obs dictionary, an obs error
        dictionary, a dataClass and a start and finish time step.
        """
        pvals = cp.copy(self.xb)
        pvals[self.dC.params4opt] = pvals_opt
        pvallist, matlist = self.linmod_list(pvals)
        hx, hmatrix = self.hmat(pvallist, matlist)
        hmat_opt = hmatrix[:, self.dC.params4opt]

        d_xb_mat = np.array([pvals_opt + np.sqrt(self.size_ens-1)*xi for xi in self.xb_mat])
        pval_ens = np.array([self.mod_list(self.long_pvals(xbi)) for xbi in d_xb_mat])
        hmx_mat = np.array([self.hxcost(pval_lst) for pval_lst in pval_ens])
        finite_mat = np.dot((1./(np.sqrt(self.size_ens-1)))*np.array([hxi - hx for hxi in hmx_mat]).T, self.xb_mat_inv)

        obcost = np.dot(finite_mat.T, np.dot(np.linalg.inv(self.rmatrix),
                                          (self.yoblist-hx).T))
        if self.modcoston is True:
            modcost = np.dot(np.linalg.inv(self.dC.opt_B), (pvals_opt-self.opt_xb).T)
        else:
            modcost = 0
        gradcost = - obcost + modcost
        return gradcost

    def acovmat(self, pvals):
        """Calculates approximation to analysis error covariance matrix
        A = (B^(-1) + H^(T) * R^(-1) * H)^(-1).
        """
        pvallist, matlist = self.linmod_list(pvals)
        hx, hmatrix = self.hmat(pvallist, matlist)
        return np.linalg.inv(np.linalg.inv(self.dC.B) + np.dot(hmatrix.T,
                        np.dot(np.linalg.inv(self.rmatrix), hmatrix)))


# ------------------------------------------------------------------------------
# CVT and implied B.
# ------------------------------------------------------------------------------

    def modcost_cvt(self, zvals):
        """model part of cost fn.
        """
        return np.dot(np.dot(zvals, np.linalg.inv(self.b_tilda)), zvals.T)

    def obcost_cvt(self, zvals):
        """Observational part of cost fn.
        """
        pvals = self.zvals2pvals(zvals)
        pvallist = self.mod_list(pvals)
        hx = self.hxcost(pvallist)
        return np.dot(np.dot((self.yoblist-hx), np.linalg.inv(self.rmatrix)), (self.yoblist-hx).T)

    def bndcost_cvt(self, zvals):
        pvals = self.zvals2pvals(zvals)
        return np.dot(np.dot((pvals-self.dC.mid_bnds), np.linalg.inv(self.dC.bnd_cov)), (pvals-self.dC.mid_bnds).T)

    def cost_cvt(self, zvals):
        """4DVAR cost function to be minimized. Takes an initial state (pvals),
        an observation dictionary, observation error dictionary, a dataClass
        and a start and finish time step.
        """
        ob_cost = self.obcost_cvt(zvals)
        if self.modcoston is True:
            mod_cost = self.modcost_cvt(zvals)
        else:
            mod_cost = 0
        cost = 0.5*ob_cost + 0.5*mod_cost  # + 0.5*self.bndcost_cvt(zvals)
        return cost

    def gradcost_cvt(self, zvals):
        """Gradient of 4DVAR cost fn to be passed to optimization routine.
        Takes an initial state (pvals), an obs dictionary, an obs error
        dictionary, a dataClass and a start and finish time step.
        """
        pvals = self.zvals2pvals(zvals)
        pvallist, matlist = self.linmod_list(pvals)
        hx, hmatrix = self.hmat(pvallist, matlist)
        obcost = np.dot(np.sqrt(self.diag_b).T, np.dot(hmatrix.T, np.dot(np.linalg.inv(self.rmatrix),
                                          (self.yoblist-hx).T)))
        if self.modcoston is True:
            modcost = np.dot(np.linalg.inv(self.b_tilda), zvals.T)
        else:
            modcost = 0
        # bnd_cost = np.dot(np.linalg.inv(self.dC.bnd_cov), (pvals-self.dC.mid_bnds).T)
        gradcost = - obcost + modcost  # + bnd_cost
        return gradcost

    def gradcost2_cvt(self, zvals):
        """Gradient of 4DVAR cost fn to be passed to optimization routine.
        Takes an initial state (pvals), an obs dictionary, an obs error
        dictionary, a dataClass and a start and finish time step. Using Lagrange
        multipliers to increase speed, method updated to allow for temporally
        correlated R matrix. Uses method of Lagrange multipliers!
        """
        pvals = self.zvals2pvals(zvals)
        pvallist, matlist = self.linmod_list(pvals)
        hx, hhat = self.hhat(pvallist)
        r_yhx = np.dot(np.linalg.inv(self.rmatrix), (self.yoblist-hx).T)
        idx1 = len(self.yoblist) - sum(self.obs_time_step[self.lenrun-1:])
        idx2 = len(self.yoblist) - sum(self.obs_time_step[self.lenrun-1+1:])
        obcost = np.dot(hhat[idx1:idx2].T, r_yhx[idx1:idx2])
        for i in xrange(self.lenrun-2, -1, -1):
            if self.obs_time_step[i] != 0:
                idx1 = len(self.yoblist) - sum(self.obs_time_step[i:])
                idx2 = len(self.yoblist) - sum(self.obs_time_step[i+1:])
                obcost = np.dot(matlist[i].T, obcost) + np.dot(hhat[idx1:idx2].T, r_yhx[idx1:idx2])
            else:
                obcost = np.dot(matlist[i].T, obcost)
        obcost = np.dot(np.sqrt(self.diag_b).T, obcost)

        if self.modcoston is True:
            modcost = np.dot(np.linalg.inv(self.b_tilda), zvals.T)
        else:
            modcost = 0
        gradcost = - obcost + modcost
        return gradcost

    def pvals2zvals(self, pvals):
        """Convert x_0 state to z_0 state for CVT with DALEC.
        """
        Bsqrt = np.linalg.inv(np.sqrt(self.diag_b))
        return np.dot(Bsqrt, (pvals - self.xb))

    def zvals2pvals(self, zvals):
        """Convert z_0 to x_0 for CVT.
        """
        return np.dot(np.sqrt(self.diag_b),zvals)+self.xb

    def zvalbnds(self, bnds):
        """Calculates bounds for transformed problem.
        """
        lower_bnds = []
        upper_bnds = []
        for t in bnds:
            lower_bnds.append(t[0])
            upper_bnds.append(t[1])
        zval_lowerbnds = self.pvals2zvals(np.array(lower_bnds))
        zval_upperbnds = self.pvals2zvals(np.array(upper_bnds))
        new_bnds=[]
        for t in xrange(len(bnds)):
            new_bnds.append((zval_lowerbnds[t],zval_upperbnds[t]))
        return tuple(new_bnds)

    def cvt_hmat(self, pvallist, matlist):
        """
        Calculates the normalised \hat{H} matrix for the CVT case
        :param pvallist: list of model evolved parameter values
        :param matlist: list of linearised models
        :return: normalised \hat{H}
        """
        hx, hmat = self.hmat(pvallist, matlist)
        obs_mat = np.dot(np.dot(np.linalg.inv(np.sqrt(self.rmatrix)), hmat), np.sqrt(self.diag_b))
        return obs_mat

    def cvt_a_covmat(self, pvals):
        """Calculates approximation to analysis error covariance matrix
        A = (B^(-1) + H^(T) * R^(-1) * H)^(-1).
        """
        pvallist, matlist = self.linmod_list(pvals)
        hx, hmatrix = self.hmat(pvallist, matlist)
        return np.linalg.inv(np.linalg.inv(self.b_tilda) + np.dot(np.sqrt(self.diag_b), np.dot(hmatrix.T,
                        np.dot(np.dot(np.linalg.inv(self.rmatrix), hmatrix), np.sqrt(self.diag_b)))))

    def cvt_sic(self, pvals):
        acovmat = self.cvt_a_covmat(pvals)
        return 0.5*np.log(np.linalg.det(self.b_tilda)/np.linalg.det(acovmat))

    def cvt_dfs(self, pvals):
        acovmat = self.cvt_a_covmat(pvals)
        return 23 - np.matrix.trace(np.dot(np.linalg.inv(self.b_tilda), acovmat))


# ------------------------------------------------------------------------------
# 4D-En-Var functions
# ------------------------------------------------------------------------------

    def long_pvals(self, pvals_opt):
        pvals = cp.copy(self.xb)
        pvals[self.dC.params4opt] = pvals_opt
        return pvals

    def create_ensemble(self, size=25):
        xbs = np.random.multivariate_normal(self.xb, self.dC.B, size=size)
        xb_mat = (1./(np.sqrt(size-1)))*np.array([xb - self.xb for xb in xbs])
        pval_ens = np.array([self.mod_list(xb) for xb in xbs])
        xb_pval_lst = self.mod_list(self.xb)
        hmx_mat = np.array([self.hxcost(pval_lst) - self.hxcost(xb_pval_lst) for pval_lst in pval_ens])
        obs_len=self.obs_time_step[self.obs_time_step > 0.]
        idxes = np.append([0], np.cumsum(obs_len))
        hmx_mat_split = [hmx_mat[:, idxes[x]:idxes[x+1]] for x in xrange(len(idxes)-1)]
        rmat_split = [self.rmatrix[idxes[x]:idxes[x+1], idxes[x]:idxes[x+1]] for x in xrange(len(idxes)-1)]
        return xb_mat, hmx_mat_split, rmat_split, idxes

    def create_ensemble2(self, size=25):
        #xbs = []
        #while len(xbs) < size:
        #    xbi = np.random.multivariate_normal(self.opt_xb, self.opt_B)
        #    if self.test_pval_opt_bnds(xbi) is True:
        #        xbs.append(xbi)
        #    else:
        #        continue
        #xbs = np.array(xbs)
        xbs = np.random.multivariate_normal(self.opt_xb, self.opt_B, size=size)
        xb_mean = self.opt_xb  # np.mean(xbs, axis=0)
        xb_mat = (1./(np.sqrt(self.size_ens-1)))*np.array([xbi - xb_mean for xbi in xbs])
        pval_ens = np.array([self.mod_list(xb) for xb in xbs])
        xb_pval_lst = self.mod_list(self.xb)
        hxb = self.hxcost(xb_pval_lst)
        hm_xbs = np.array([self.hxcost(pval_lst) for pval_lst in pval_ens])
        hmx_mat = (1./(np.sqrt(self.size_ens-1)))*np.array([hmxb - hxb for hmxb in hm_xbs])
        return xbs, xb_mat, hxb, hm_xbs, hmx_mat

    def run_ensemble(self, xb_opt):
        self.opt_xb = xb_opt
        self.size_ens = self.size_ens
        self.xbs, self.xb_mat, self.hxb, self.hm_xbs, self.hmx_mat = self.create_ensemble2(self.size_ens)
        self.xb_mat_inv = np.linalg.pinv(self.xb_mat.T)
        return 'done'

    def run_ensemble_wvals(self, wvals):
        self.opt_xb = self.wvals2xvals(wvals)
        self.opt_B = self.a_cov_mat_ens()
        self.size_ens = self.size_ens
        self.xbs, self.xb_mat, self.hxb, self.hm_xbs, self.hmx_mat = self.create_ensemble2(self.size_ens)
        self.xb_mat_inv = np.linalg.pinv(self.xb_mat.T)
        return 'done'

    def xvals2wvals(self, xvals):
        # return np.dot(self.xb_mat_inv, (xvals - np.mean(self.xbs, axis=0)))
        return np.dot(self.xb_mat_inv, (xvals - self.opt_xb))

    def wvals2xvals(self, wvals):
        # return np.mean(self.xbs, axis=0) + np.dot(self.xb_mat.T, wvals)
        return self.opt_xb + np.dot(self.xb_mat.T, wvals)

    def obcost_ens(self, wvals):
        """Observational part of cost fn.
        """
        pvals = self.wvals2xvals(wvals)
        pvallist = self.mod_list(self.long_pvals(pvals))
        hx = self.hxcost(pvallist)
        return np.dot(np.dot((self.yoblist - hx), np.linalg.inv(self.rmatrix)), (self.yoblist - hx).T)

    def obcost_ens_inc(self, wvals):
        """Observational part of cost fn.
        """
        #pvals = self.wvals2xvals(wvals)
        #pvallist = self.mod_list(self.long_pvals(pvals))
        #hx = self.hxcost(pvallist)
        #d_xb_mat = np.array([pvals + np.sqrt(self.size_ens-1)*xi for xi in self.xb_mat])
        #pval_ens = np.array([self.mod_list(self.long_pvals(xbi)) for xbi in d_xb_mat])
        #hmx_mat = np.array([self.hxcost(pval_lst) for pval_lst in pval_ens])
        #finite_mat = (1./(np.sqrt(self.size_ens-1)))*np.array([hxi - hx for hxi in hmx_mat])
        #return np.dot(np.dot((np.dot(finite_mat.T, wvals) + hx - self.yoblist), np.linalg.inv(self.rmatrix)),
        #              (np.dot(finite_mat.T, wvals) + hx - self.yoblist).T)

    def cost_ens(self, wvals):
        modcost = (self.size_ens - 1) * np.dot(wvals, wvals.T)
        obcost = self.obcost_ens(wvals)
        ret_val = 0.5 * modcost + 0.5 * obcost
        # print 'cost: %d' %ret_val
        return ret_val

    def cost_ens_inc(self, wvals):
        modcost = np.dot(wvals, wvals.T)
        obcost = self.obcost_ens_inc(wvals)
        #if self.test_pval_opt_bnds(self.wvals2xvals(wvals)) is False:
        #    ret_val = 1e10
        #else:
        ret_val = 0.5 * modcost + 0.5*obcost
        # print 'cost: %d' %ret_val
        return ret_val

    def gradcost_ens(self, wvals, fact=1e-3):
        """Gradient of 4DVAR cost fn to be passed to optimization routine.
        Takes an initial state (pvals), an obs dictionary, an obs error
        dictionary, a dataClass and a start and finish time step.
        """
        pvals = self.wvals2xvals(wvals)
        esp = fact
        pvallist = self.mod_list(self.long_pvals(pvals))
        hx = self.hxcost(pvallist)
        d_xb_mat = np.array([pvals + xi for xi in self.xb_mat])
        pval_ens = np.array([self.mod_list(self.long_pvals(xbi)) for xbi in d_xb_mat])
        hmx_mat = np.array([self.hxcost(pval_lst) for pval_lst in pval_ens])
        finite_mat = (1./(np.sqrt(self.size_ens-1)))*np.array([hxi - np.mean(hmx_mat, axis=0) for hxi in hmx_mat])
        obcost = np.dot(finite_mat, np.dot(np.linalg.inv(self.rmatrix),
                                          (hx - self.yoblist).T))

        gradcost = - obcost + (self.size_ens-1) * wvals
        return gradcost

    def hesscost_ens(self, wvals, fact=1e-3):
        pvals = self.wvals2xvals(wvals)
        esp = np.sqrt(self.size_ens - 1) * fact
        d_xb_mat = np.array([pvals + esp*xi for xi in self.xb_mat])
        pval_ens = np.array([self.mod_list(self.long_pvals(xbi)) for xbi in d_xb_mat])
        hmx_mat = np.array([self.hxcost(pval_lst) for pval_lst in pval_ens])
        finite_mat = (1. / esp) * np.array([hxi - np.mean(hmx_mat, axis=0) for hxi in hmx_mat])
        hess = (self.size_ens - 1) * np.eye(self.size_ens) + \
               np.dot(finite_mat, np.dot(np.linalg.inv(self.rmatrix), finite_mat.T))
        return hess

    def minim(self, wvals, tol=1e-1, j_max=20):
        gradj = self.gradcost_ens(wvals)
        hessj = self.hesscost_ens(wvals)
        delt_w = np.dot(np.linalg.inv(hessj), gradj)
        j = 0
        while np.linalg.norm(delt_w) >= tol and j < j_max:
            wvals = wvals + delt_w
            gradj = self.gradcost_ens(wvals)
            hessj = self.hesscost_ens(wvals)
            delt_w = np.dot(np.linalg.inv(hessj), gradj)
            print np.linalg.norm(delt_w)
            j += 1
        return wvals, self.wvals2xvals(wvals)


    def gradcost_ens_inc(self, wvals):
        """Gradient of 4DVAR cost fn to be passed to optimization routine.
        Takes an initial state (pvals), an obs dictionary, an obs error
        dictionary, a dataClass and a start and finish time step.
        """
        pvals = self.wvals2xvals(wvals)
        pvallist = self.mod_list(self.long_pvals(pvals))
        hx = self.hxcost(pvallist)
        d_xb_mat = np.array([pvals + np.sqrt(self.size_ens-1)*xi for xi in self.xb_mat])
        pval_ens = np.array([self.mod_list(self.long_pvals(xbi)) for xbi in d_xb_mat])
        hmx_mat = np.array([self.hxcost(pval_lst) for pval_lst in pval_ens])
        finite_mat = (1./(np.sqrt(self.size_ens-1)))*np.array([hxi - hx for hxi in hmx_mat])
        obcost = np.dot(finite_mat, np.dot(np.linalg.inv(self.rmatrix),
                        (np.dot(finite_mat.T, wvals) + hx - self.yoblist).T))

        gradcost = obcost + wvals  # + bnd_cost
        return gradcost

    def gradcost2_ens(self, wvals):
        """Gradient of 4DVAR cost fn to be passed to optimization routine.
        Takes an initial state (pvals), an obs dictionary, an obs error
        dictionary, a dataClass and a start and finish time step. Using Lagrange
        multipliers to increase speed, method updated to allow for temporally
        correlated R matrix. Uses method of Lagrange multipliers!
        """
        pvals = self.wvals2xvals(wvals)
        pvallist = self.mod_list(self.long_pvals(pvals))
        hx = self.hxcost(pvallist)
        r_yhx = np.dot(np.linalg.inv(self.rmatrix), (hx - self.yoblist).T)
        idx1 = len(self.yoblist) - sum(self.obs_time_step[self.lenrun-1:])
        idx2 = len(self.yoblist) - sum(self.obs_time_step[self.lenrun-1+1:])
        obcost = np.dot(self.hmx_mat[:, idx1:idx2], r_yhx[idx1:idx2])
        for i in xrange(self.lenrun-2, -1, -1):
            if self.obs_time_step[i] != 0:
                idx1 = len(self.yoblist) - sum(self.obs_time_step[i:])
                idx2 = len(self.yoblist) - sum(self.obs_time_step[i+1:])
                obcost = obcost + np.dot(self.hmx_mat[:, idx1:idx2], r_yhx[idx1:idx2])
            else:
                obcost = obcost
        obcost = obcost
        modcost = (self.size_ens-1) * wvals
        gradcost = obcost + modcost
        return gradcost

    def wvalbnds(self, bnds):
        """Calculates bounds for transformed problem.
        """
        lower_bnds = []
        upper_bnds = []
        for t in bnds:
            lower_bnds.append(t[0])
            upper_bnds.append(t[1])
        wval_lowerbnds = self.xvals2wvals(np.array(lower_bnds))
        wval_upperbnds = self.xvals2wvals(np.array(upper_bnds))
        new_bnds = []
        for t in xrange(len(wval_lowerbnds)):
            new_bnds.append((min(wval_lowerbnds[t],wval_upperbnds[t]), max(wval_lowerbnds[t],wval_upperbnds[t])))
        return tuple(new_bnds)

    def test_pval_opt_bnds(self, pvals):
        """Tests pvals to see if they are within the correct bnds.
        """
        x = 0
        for bnd in self.dC.opt_bnds:
            if bnd[0] < pvals[x] < bnd[1]:
                x += 1
            else:
                return False
        return True

# ------------------------------------------------------------------------------
# Minimization Routines.
# ------------------------------------------------------------------------------

    def find_min_tnc(self, pvals_opt, pvals, size_ens=24, bnds='strict', dispp=None, maxits=2000,
                     mini=0, f_tol=-1):
        """Function which minimizes 4DVAR cost fn. Takes an initial state
        (pvals).
        """
        self.xb = pvals
        self.opt_xb = pvals[self.dC.params4opt]
        self.size_ens = size_ens
        self.run_ensemble(self.opt_xb)
        if bnds == 'strict':
            bnds = self.dC.opt_bnds
        else:
            bnds = bnds
        find_min = spop.fmin_tnc(self.cost, pvals_opt, fprime=self.gradcost,
                                 bounds=bnds, disp=dispp, fmin=mini, maxfun=maxits, ftol=f_tol)
        xa = cp.copy(pvals)
        xa[self.dC.params4opt] = find_min[0]
        return find_min, xa

    def find_min_tnc_cvt(self, pvals, f_name='None', email=0, bnds='strict', dispp=5, maxits=2000,
                         mini=0, f_tol=1e-4):
        """Function which minimizes 4DVAR cost fn. Takes an initial state
        (pvals).
        """
        self.xb = pvals
        if bnds == 'strict':
            bnds = self.zvalbnds(self.dC.bnds_tst)
        else:
            bnds = bnds
        zvals = self.pvals2zvals(pvals)
        find_min = spop.fmin_tnc(self.cost_cvt, zvals,
                                fprime=self.gradcost2_cvt, bounds=bnds,
                                disp=dispp, fmin=mini, maxfun=maxits, ftol=f_tol)
        xa = self.zvals2pvals(find_min[0])
        if f_name != None:
            self.pickle_exp(pvals, find_min, xa, f_name)
        return find_min, xa

    def find_min_tnc_ens(self, pvals_opt, pvals, size_ens=50, f_name='None', dispp=5, maxits=2000,
                         mini=0, f_tol=1e-4):
        """Function which minimizes 4DVAR cost fn. Takes an initial state
        (pvals).
        """
        self.xb = pvals
        self.opt_xb = pvals[self.dC.params4opt]
        self.size_ens = size_ens
        self.run_ensemble(self.opt_xb)
        wvals = np.zeros(self.size_ens)
        find_min = spop.fmin_tnc(self.cost_ens, wvals,
                                 fprime=self.gradcost_ens,
                                 disp=dispp, fmin=mini, maxfun=maxits, ftol=f_tol)
        xa = cp.copy(pvals)
        xa[self.dC.params4opt] = self.wvals2xvals(find_min[0])
        if f_name != None:
            self.pickle_exp(pvals, find_min, xa, f_name)
        return find_min, xa

    def find_min_tnc_ens_inc(self, pvals_opt, pvals, size_ens=50, f_name='None', dispp=5, maxits=2000,
                         mini=0, f_tol=1e-4):
        """Function which minimizes 4DVAR cost fn. Takes an initial state
        (pvals).
        """
        self.xb = pvals
        self.opt_xb = pvals[self.dC.params4opt]
        self.size_ens = size_ens
        self.run_ensemble(self.opt_xb)
        wvals = self.xvals2wvals(pvals_opt)
        find_min = spop.fmin_tnc(self.cost_ens_inc, wvals,
                                fprime=self.gradcost_ens_inc,
                                disp=dispp, fmin=mini, maxfun=maxits, ftol=f_tol)
        xa = cp.copy(pvals)
        xa[self.dC.params4opt] = self.wvals2xvals(find_min[0])
        if f_name != None:
            self.pickle_exp(pvals, find_min, xa, f_name)
        return find_min, xa

    def findminglob_ens(self, pvals, meth='TNC', bnds='strict', it=300,
                    stpsize=0.5, temp=1., displ=True, maxits=3000):
        """Function which minimizes 4DVAR cost fn. Takes an initial state
        (pvals), an obs dictionary, an obs error dictionary, a dataClass and
        a start and finish time step.
        """
        self.xb = pvals
        wvals = self.xvals2wvals(pvals)
        findmin = spop.basinhopping(self.cost_ens, wvals, niter=it,
                                    minimizer_kwargs={'method': meth,
                                                      'jac': self.gradcost2_ens,
                                                      'options': {'maxiter': maxits}},
                                    stepsize=stpsize, T=temp, disp=displ)
        xa = self.wvals2xvals(findmin[0])
        return findmin, xa

    def findminglob(self, pvals, meth='TNC', bnds='strict', it=300,
                    stpsize=0.5, temp=1., displ=True, maxits=3000):
        """Function which minimizes 4DVAR cost fn. Takes an initial state
        (pvals), an obs dictionary, an obs error dictionary, a dataClass and
        a start and finish time step.
        """
        if bnds == 'strict':
            bnds = self.dC.bnds
        else:
            bnds = bnds
        findmin = spop.basinhopping(self.cost, pvals, niter=it,
                                    minimizer_kwargs={'method': meth, 'bounds': bnds,
                                                      'jac': self.gradcost2,
                                                      'options': {'maxiter': maxits}},
                                    stepsize=stpsize, T=temp, disp=displ)
        return findmin

    def ensemble(self, pvals):
        """Ensemble 4DVAR run for twin experiments.
        """
        ensempvals = np.ones((self.nume, 23))
        for x in xrange(self.nume):
            ensempvals[x] = self.dC.randompert(pvals)

        assim_results = [self.find_min_tnc(ensemp, dispp=False) for ensemp in
                         ensempvals]

        xalist = [assim_results[x][0] for x in xrange(self.nume)]

        return ensempvals, xalist, assim_results

    def var_ens(self, size_ens=10):
        edc_ens = pickle.load(open('misc/edc_param_ensem.p', 'r'))
        param_ens = rand.sample(edc_ens, size_ens)
        num_cores = multiprocessing.cpu_count()
        output = jl.Parallel(n_jobs=num_cores)(jl.delayed(self.find_min_tnc_cvt)(self, pval) for pval in param_ens)
        return output


# ------------------------------------------------------------------------------
# Cycled 4D-Var.
# ------------------------------------------------------------------------------

    def cycle_4dvar(self, pvals, lenwind, numbwind, lenrun):
        """Cycle 4Dvar windows and see their effect on predicting future obs.
        """
        xb = [pvals]
        xa = []
        self.startrun = 0
        self.endrun = lenwind
        for x in xrange(numbwind):
            self.yoblist, self.yerroblist, ytimstep, y_strlst = self.obscost()
            self.rmatrix = self.rmat(self.yerroblist)
            xa.append(self.find_min_tnc(xb[x]))
            xb.append(self.mod_list(xa[x][0])[self.endrun-self.startrun])
            self.startrun += lenwind
            self.endrun += lenwind

        self.startrun -= lenwind*numbwind
        self.endrun -= lenwind*numbwind
        conditions = {'pvals': pvals, 'lenwind': lenwind, 'numbwind': numbwind,
                      'lenrun': lenrun}
        return conditions, xb, xa

    def yearly_cycle4dvar(self, pvals):
        """
        Performs cycle DA with windows of year length
        :param pvals: Initial background vector for first assim. window
        :return: list of all xb vectors, list of all xa vectors and minimisation
        output.
        """
        year_lst = np.unique(self.dC.year)
        xb = [pvals]
        xa = []
        b_cov = []
        for year in enumerate(year_lst):
            year_idx = np.where(self.dC.year == year[1])[0]
            self.startrun = year_idx[0]
            self.endrun = year_idx[-1]
            self.lenrun = self.endrun - self.startrun
            self.obs_time_step = self.no_obs_at_time()
            self.yoblist, self.yerroblist, ytimestep, y_strlst = self.obscost()
            self.rmatrix = self.rmat(self.yerroblist)
            xa.append(self.find_min_tnc_cvt(pvals, f_tol=1e-2))
            acovmat = self.acovmat(xa[year[0]][1])
            b_cov.append(acovmat)
            self.endrun += 1
            pvallst, matlist = self.linmod_list(xa[year[0]][1])
            xb.append(pvallst[-1])
            ev_acovmat = 1.2 * self.evolve_mat(acovmat, matlist)
            # ev_acovmat = self.dC.B
            ev_acovmat[11, 11] = self.dC.B[11, 11]
            ev_acovmat[13, 13] = self.dC.B[13, 13]
            ev_acovmat[14, 14] = self.dC.B[14, 14]
            ev_acovmat[15, 15] = self.dC.B[15, 15]
            self.diag_b = np.diag(np.diag(ev_acovmat))
            self.b_tilda = np.dot(np.dot(np.linalg.inv(np.sqrt(self.diag_b)),ev_acovmat),
                                  np.linalg.inv(np.sqrt(self.diag_b)))
            print xa[year[0]][1]
            #Change B too, use corr B to start with then evolve A with M
            #for newB. Figure how to create corrR with multiple data streams.

        self.startrun = 0
        self.endrun = self.lenrun
        self.diag_b = np.diag(np.diag(self.dC.B))
        self.b_tilda = np.dot(np.dot(np.linalg.inv(np.sqrt(self.diag_b)),self.dC.B),np.linalg.inv(np.sqrt(self.diag_b)))
        return xb, xa, b_cov


# ------------------------------------------------------------------------------
# MCMC
# ------------------------------------------------------------------------------

    def randompvals(self, nwalkers):
        """Creates a random list of parameter values for dalec using dataClass
        bounds.
        """
        rndpvals = np.ones((nwalkers, 23))*-9999.
        x=0
        for t in xrange(nwalkers):
            for bnd in self.dC.bnds:
                rndpvals[t, x] = np.random.uniform(bnd[0],bnd[1])
                x += 1
            x-=23

        return rndpvals

    def log_prior(self, pvals):
        tick = 23
        for x in xrange(23):
            if self.dC.bnds[x][0]<pvals[x]<self.dC.bnds[x][1]:
                tick -= 1
        if tick==0:
            return 0.0
        return -np.inf

    def log_posterior(self, pvals):
        lp = self.log_prior(pvals)
        if not np.isfinite(lp):
            return -np.inf
        return self.log_prior(pvals) - self.obcost(pvals)

    def mcmc_run(self, pvals, ndim=23, nwalkers=50, nsteps=10000):
        """
        ndim = 23  # number of parameters in the model
        nwalkers = 50 # number of MCMC walkers
        nburn = 1000  # "burn-in" period to let chains stabilize
        nsteps = 10000  # number of MCMC steps to take
        """
        sampler = emcee.EnsembleSampler(nwalkers, ndim, self.log_posterior)
        sampler.run_mcmc(pvals, nsteps)
        return sampler

    def mean_pval(self, sampler, nburn=1000):
        """
        nburn = 1000  # "burn-in" period to let chains stabilize
        """
        sample = sampler.chain[:, nburn:, :].reshape(-1, 23)
        return sample.mean(0)

    def tstpvals(self, pvals, bnds):
        """Tests pvals to see if they are within the correct bnds.
        """
        x=0
        for bnd in bnds:
            if bnd[0]<pvals[x]<bnd[1]:
                print '%f in bnds' %x
            else:
                print '%f not in bnds' %x
            x += 1
        return pvals

    def tstpvals2(self, pvals):
        tick = 0
        for x in xrange(23):
            if self.dC.bnds5[x][0] >= pvals[x] >= self.dC.bnds5[x][1]:
                print '%f' %x
                tick -= 1
        if tick != 0:
            return np.inf


# ------------------------------------------------------------------------------
# Misc
# ------------------------------------------------------------------------------

    def pickle_obs(self, f_name):
        obs = {}
        obs['obs'] = self.dC.ob_dict
        obs['obs_err'] = self.dC.ob_err_dict
        f = open('obs_exps/'+f_name, 'w')
        pickle.dump(obs, f)
        f.close()
        return 'Observations and error dictionaries pickled!'

    def pickle_exp(self, xb, assim_res, xa, f_name):
        exp = {}
        exp['obs'] = self.dC.ob_dict
        exp['obs_err'] = self.dC.ob_err_dict
        exp['b_mat'] = self.dC.B
        exp['xb'] = xb
        exp['assim_res'] = assim_res
        exp['xa'] = xa
        exp['rmat'] = self.rmatrix
        f = open(f_name, 'w')
        pickle.dump(exp, f)
        f.close()
        return 'Experiment assimilation results pickled!'

    def exp_dict(self, xb, assim_res, xa):
        exp = {}
        exp['obs'] = self.dC.ob_dict
        exp['obs_err'] = self.dC.ob_err_dict
        exp['b_mat'] = self.dC.B
        exp['xb'] = xb
        exp['assim_res'] = assim_res
        exp['xa'] = xa
        return exp


